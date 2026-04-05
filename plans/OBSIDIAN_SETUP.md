# Obsidian Setup Guide

Complete these steps to set up your wiki as an Obsidian vault. Takes ~10 minutes.

---

## Step 1: Open the Vault

1. Download [Obsidian](https://obsidian.md) if you haven't already
2. Open Obsidian
3. Click **"Open folder as vault"**
4. Navigate to `/Users/drdileepunni/github_/llm_wiki` and select it
5. Obsidian will create a `.obsidian/` folder (this is hidden, don't modify it)

---

## Step 2: Install Community Plugins

Go to **Settings → Community Plugins**

### Plugin 1: Dataview
1. Click **Browse**
2. Search for **Dataview**
3. Install and Enable
4. Use this to query page frontmatter as a database (optional but powerful)

### Plugin 2: Obsidian Git
1. Click **Browse**
2. Search for **Obsidian Git**
3. Install and Enable
4. Optional: configure auto-commit settings (Settings → Obsidian Git)
   - Suggested: auto-commit every 5 minutes

---

## Step 3: Configure Attachment Location

Go to **Settings → Files and links**

Find: "Default location for new attachments"

Set to: `raw/assets/`

This ensures any images you clip or paste go to the right place.

---

## Step 4: Install Web Clipper (Browser)

This lets you clip articles directly into `raw/` as markdown.

- **Chrome/Edge**: [Obsidian Web Clipper](https://chrome.google.com/webstore/detail/obsidian-web-clipper/)
- **Firefox**: [Obsidian Web Clipper](https://addons.mozilla.org/firefox/addon/obsidian-web-clipper/)

After install:
1. Click the clipper extension icon
2. Sign in with your Obsidian account (or local vault)
3. Set default save location to `raw/`

---

## Step 5: Explore Graph View

Click the **Graph View** icon in the left sidebar (looks like a network diagram).

As you ingest sources, this will show how your wiki interconnects. It starts empty but grows visually as you add pages.

---

## You're All Set!

Go back to Claude Code and say: **"Obsidian is ready. Here's my first source:"**

Then paste or reference the source you want to ingest.
