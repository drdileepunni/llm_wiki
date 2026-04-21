const BASE = '/api'

function kbHeaders(kbName) {
  return kbName ? { 'X-KB-Name': kbName } : {}
}

export async function ingestFile(file, kbName) {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/ingest/file`, {
    method: 'POST',
    body: form,
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function ingestPubmed(pmid, kbName) {
  const form = new FormData()
  form.append('pmid', pmid)
  const res = await fetch(`${BASE}/ingest/pubmed`, {
    method: 'POST',
    body: form,
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function ingestUrl(url, kbName) {
  const form = new FormData()
  form.append('url', url)
  const res = await fetch(`${BASE}/ingest/url`, {
    method: 'POST',
    body: form,
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function sendChat(question, kbName) {
  const res = await fetch(`${BASE}/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ question }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function fileAnswer(question, answer, kbName) {
  const res = await fetch(`${BASE}/chat/file`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
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

export async function getWikiTree(kbName) {
  return fetch(`${BASE}/wiki/tree`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function getWikiFile(path, kbName) {
  return fetch(`${BASE}/wiki/file?path=${encodeURIComponent(path)}`, {
    headers: kbHeaders(kbName),
  }).then(r => r.json())
}

export async function searchWiki(q, kbName) {
  return fetch(`${BASE}/wiki/search?q=${encodeURIComponent(q)}`, {
    headers: kbHeaders(kbName),
  }).then(r => r.json())
}

export async function saveWikiFile(path, content, kbName) {
  const res = await fetch(`${BASE}/wiki/file`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ path, content }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// ── KB management ──────────────────────────────────────────────────────────────

export async function listKBs() {
  return fetch(`${BASE}/kbs/`).then(r => r.json())
}

export async function createKB(name) {
  const res = await fetch(`${BASE}/kbs/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getKBPrompt(name) {
  return fetch(`${BASE}/kbs/${encodeURIComponent(name)}/prompt`).then(r => r.json())
}

export async function updateKBPrompt(name, content) {
  const res = await fetch(`${BASE}/kbs/${encodeURIComponent(name)}/prompt`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getWikiGaps(kbName) {
  return fetch(`${BASE}/wiki/gaps`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

// ── Gap resolver ───────────────────────────────────────────────────────────────

export async function resolveSearch(gapTitle, gapSections, kbName, maxResults = 5) {
  const res = await fetch(`${BASE}/resolve/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ gap_title: gapTitle, gap_sections: gapSections, max_results: maxResults }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function resolveIngest(pmcId, title, citation, kbName) {
  const res = await fetch(`${BASE}/resolve/ingest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ pmc_id: pmcId, title, citation: citation || '' }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function resolveJobStatus(jobId) {
  return fetch(`${BASE}/resolve/jobs/${jobId}`).then(r => r.json())
}

export async function clearWikiContents(kbName) {
  const res = await fetch(`${BASE}/wiki/contents`, {
    method: 'DELETE',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
