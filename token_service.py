"""
Token management service for handling bearer token generation and refresh.
This service automatically manages bearer tokens using refresh tokens from environment variables.
"""

import os
import json
import base64
import requests
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class TokenService:
    """
    Service for managing bearer tokens with automatic refresh functionality.
    
    This service:
    1. Gets refresh tokens from environment variables (.env file)
    2. Uses refresh tokens to generate bearer tokens
    3. Automatically refreshes tokens when they expire
    4. Provides thread-safe access to current bearer token
    """
    
    def __init__(self):
        self._bearer_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._lock = threading.Lock()
        self._refresh_token: Optional[str] = None
        self._radar_post_url: Optional[str] = None
        self._is_initialized = False
        
    def initialize(self) -> bool:
        """
        Initialize the token service by loading configuration from environment variables.
        
        Returns:
            bool: True if initialization successful, False otherwise
        """
        try:
            # Get refresh token from environment (loaded from .env file)
            self._refresh_token = os.getenv('REFRESH_TOKEN')
            # Support both RADAR_POST_URL and RADAR_POST_URL_STAGING
            self._radar_post_url = os.getenv('RADAR_POST_URL') or os.getenv('RADAR_POST_URL_STAGING')
            
            if not self._refresh_token:
                logger.error("REFRESH_TOKEN not found in environment variables")
                return False
                
            if not self._radar_post_url:
                logger.error("RADAR_POST_URL or RADAR_POST_URL_STAGING not found in environment variables")
                return False
            
            logger.info("Token service initialized successfully")
            self._is_initialized = True
            
            # Try to get initial bearer token
            return self._refresh_bearer_token()
            
        except Exception as e:
            logger.error(f"Failed to initialize token service: {e}")
            return False
    
    def get_bearer_token(self) -> Optional[str]:
        """
        Get the current bearer token, refreshing if necessary.
        
        Returns:
            Optional[str]: Current bearer token or None if unavailable
        """
        if not self._is_initialized:
            logger.error("Token service not initialized")
            return None
            
        with self._lock:
            # Check if token needs refresh
            if self._needs_refresh():
                logger.info("Bearer token needs refresh")
                if not self._refresh_bearer_token():
                    logger.error("Failed to refresh bearer token")
                    return None
            
            return self._bearer_token
    
    def _needs_refresh(self) -> bool:
        """
        Check if the current bearer token needs to be refreshed.
        
        Returns:
            bool: True if token needs refresh, False otherwise
        """
        if not self._bearer_token or not self._token_expires_at:
            return True
            
        # Refresh token 5 minutes before expiration
        refresh_threshold = datetime.now() + timedelta(minutes=5)
        return self._token_expires_at <= refresh_threshold
    
    def _refresh_bearer_token(self) -> bool:
        """
        Refresh the bearer token using the refresh token.
        
        Returns:
            bool: True if refresh successful, False otherwise
        """
        try:
            logger.info("Refreshing bearer token...")
            
            # Construct the endpoint URL
            endpoint = f"{self._radar_post_url}/api/users/requestAccessToken?x-refresh-token={self._refresh_token}"
            
            # Prepare headers
            headers = {
                'accept': 'application/json, text/plain, */*',
                'accept-language': 'en-US,en;q=0.9',
                'content-type': 'application/json',
                'origin': 'https://staging.cloudphysicianworld.com',
                'referer': 'https://staging.cloudphysicianworld.com/',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'
            }
            
            # Make the request - try POST first, then GET
            response = None
            try:
                response = requests.post(endpoint, headers=headers, timeout=30)
                if response.status_code == 200 and 'application/json' in response.headers.get('content-type', ''):
                    pass
                else:
                    response = requests.get(endpoint, headers=headers, timeout=30)
            except Exception as e:
                logger.warning(f"POST failed: {e}, trying GET...")
                response = requests.get(endpoint, headers=headers, timeout=30)
            
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    
                    # Extract the new bearer token
                    new_token = None
                    if 'token' in response_data:
                        new_token = response_data['token']
                    elif 'access_token' in response_data:
                        new_token = response_data['access_token']
                    elif 'bearer_token' in response_data:
                        new_token = response_data['bearer_token']
                    
                    if new_token:
                        # Parse token expiration
                        expires_at = self._parse_token_expiration(new_token)
                        
                        # Update token and expiration
                        self._bearer_token = new_token
                        self._token_expires_at = expires_at
                        
                        logger.info(f"Bearer token refreshed successfully. Expires at: {expires_at}")
                        return True
                    else:
                        logger.error("Response doesn't contain a token field")
                        return False
                        
                except json.JSONDecodeError:
                    logger.error(f"Response is not valid JSON. Raw response: {response.text}")
                    return False
            else:
                logger.error(f"Token refresh request failed with status code: {response.status_code}")
                logger.error(f"Response text: {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Token refresh request failed with error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during token refresh: {e}")
            return False
    
    def _parse_token_expiration(self, token: str) -> Optional[datetime]:
        """
        Parse JWT token to extract expiration time.
        
        Args:
            token: JWT token string
            
        Returns:
            Optional[datetime]: Token expiration time or None if parsing fails
        """
        try:
            # JWT has 3 parts separated by dots
            parts = token.split('.')
            if len(parts) != 3:
                logger.warning("Invalid JWT format")
                return None
            
            # Decode the payload (second part)
            payload = parts[1]
            # Add padding if needed
            payload += '=' * (4 - len(payload) % 4)
            decoded_payload = base64.urlsafe_b64decode(payload)
            payload_data = json.loads(decoded_payload)
            
            # Check expiration
            if 'exp' in payload_data:
                exp_timestamp = payload_data['exp']
                return datetime.fromtimestamp(exp_timestamp)
            else:
                logger.warning("No expiration time found in token")
                return None
                
        except Exception as e:
            logger.error(f"Error parsing token expiration: {e}")
            return None
    
    def get_token_info(self) -> Dict[str, Any]:
        """
        Get information about the current token status.
        
        Returns:
            Dict[str, Any]: Token information including status, expiration, etc.
        """
        with self._lock:
            info = {
                'is_initialized': self._is_initialized,
                'has_token': bool(self._bearer_token),
                'expires_at': self._token_expires_at.isoformat() if self._token_expires_at else None,
                'needs_refresh': self._needs_refresh(),
                'token_preview': self._bearer_token[:20] + '...' if self._bearer_token else None
            }
            
            if self._token_expires_at:
                current_time = datetime.now()
                if current_time < self._token_expires_at:
                    time_left = self._token_expires_at - current_time
                    info['time_until_expiry'] = str(time_left)
                else:
                    info['time_until_expiry'] = 'EXPIRED'
            
            return info
    
    def force_refresh(self) -> bool:
        """
        Force a token refresh regardless of expiration status.
        
        Returns:
            bool: True if refresh successful, False otherwise
        """
        logger.info("Forcing token refresh...")
        return self._refresh_bearer_token()


# Global token service instance
_token_service = None
_service_lock = threading.Lock()


def get_token_service() -> TokenService:
    """
    Get the global token service instance (singleton pattern).
    
    Returns:
        TokenService: The global token service instance
    """
    global _token_service
    
    if _token_service is None:
        with _service_lock:
            if _token_service is None:
                _token_service = TokenService()
                _token_service.initialize()
    
    return _token_service


def get_bearer_token() -> Optional[str]:
    """
    Convenience function to get the current bearer token.
    
    Returns:
        Optional[str]: Current bearer token or None if unavailable
    """
    return get_token_service().get_bearer_token()


def get_token_info() -> Dict[str, Any]:
    """
    Convenience function to get token information.
    
    Returns:
        Dict[str, Any]: Token information
    """
    return get_token_service().get_token_info()
