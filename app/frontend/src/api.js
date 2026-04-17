const BASE = '/api'

export async function ingestFile(file) {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/ingest/file`, { method: 'POST', body: form })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function ingestPubmed(pmid) {
  const form = new FormData()
  form.append('pmid', pmid)
  const res = await fetch(`${BASE}/ingest/pubmed`, { method: 'POST', body: form })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function ingestUrl(url) {
  const form = new FormData()
  form.append('url', url)
  const res = await fetch(`${BASE}/ingest/url`, { method: 'POST', body: form })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function sendChat(question) {
  const res = await fetch(`${BASE}/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function fileAnswer(question, answer) {
  const res = await fetch(`${BASE}/chat/file`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, answer }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getStats() {
  return fetch(`${BASE}/dashboard/stats`).then(r => r.json())
}

export async function getLog() {
  return fetch(`${BASE}/dashboard/log`).then(r => r.json())
}

export async function getTimeseries() {
  return fetch(`${BASE}/dashboard/timeseries`).then(r => r.json())
}

export async function getWikiTree() {
  return fetch(`${BASE}/wiki/tree`).then(r => r.json())
}

export async function getWikiFile(path) {
  return fetch(`${BASE}/wiki/file?path=${encodeURIComponent(path)}`).then(r => r.json())
}

export async function searchWiki(q) {
  return fetch(`${BASE}/wiki/search?q=${encodeURIComponent(q)}`).then(r => r.json())
}

export async function saveWikiFile(path, content) {
  const res = await fetch(`${BASE}/wiki/file`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
