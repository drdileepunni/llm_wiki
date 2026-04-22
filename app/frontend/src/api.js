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

export async function sendChat(question, kbName, images = []) {
  const res = await fetch(`${BASE}/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ question, images }),
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

export async function resolveIngest(pmcId, title, citation, gapFile, referencedPage, gapSections, gapTitle, kbName) {
  const res = await fetch(`${BASE}/resolve/ingest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({
      pmc_id: pmcId,
      title,
      citation: citation || '',
      gap_file: gapFile || '',
      referenced_page: referencedPage || '',
      gap_sections: gapSections || [],
      gap_title: gapTitle || '',
    }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function resolveJobStatus(jobId) {
  return fetch(`${BASE}/resolve/jobs/${jobId}`).then(r => r.json())
}

export async function resolveAll(kbName, maxResults = 3) {
  const res = await fetch(`${BASE}/resolve/resolve-all`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ max_results: maxResults }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function resolveBatchStatus(batchId) {
  return fetch(`${BASE}/resolve/batch/${batchId}`).then(r => r.json())
}

export async function trackQueryAsGap(question, answer, kbName) {
  const res = await fetch(`${BASE}/resolve/gap-from-query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ question, answer }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function clearWikiContents(kbName) {
  const res = await fetch(`${BASE}/wiki/contents`, {
    method: 'DELETE',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// ── Assessment ─────────────────────────────────────────────────────────────────

export async function listAssessments(kbName) {
  return fetch(`${BASE}/assess/`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function getAssessment(sourceSlug, kbName) {
  return fetch(`${BASE}/assess/${encodeURIComponent(sourceSlug)}`, {
    headers: kbHeaders(kbName),
  }).then(r => r.json())
}

export async function runAssessment(sourceSlug, kbName) {
  const res = await fetch(`${BASE}/assess/${encodeURIComponent(sourceSlug)}/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function rateQuestion(sourceSlug, questionId, rating, kbName) {
  const res = await fetch(`${BASE}/assess/${encodeURIComponent(sourceSlug)}/rate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ question_id: questionId, rating }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function assessJobStatus(jobId) {
  return fetch(`${BASE}/assess/jobs/${jobId}`).then(r => r.json())
}

// ── Clinical Assessment ────────────────────────────────────────────────────────

export async function runClinicalAssessment(patientDir, kbName) {
  const res = await fetch(`${BASE}/clinical-assess/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ patient_dir: patientDir }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function clinicalAssessJobStatus(jobId) {
  return fetch(`${BASE}/clinical-assess/jobs/${jobId}`).then(r => r.json())
}

export async function listClinicalAssessments(kbName) {
  return fetch(`${BASE}/clinical-assess/`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function getClinicalAssessment(patientId, kbName) {
  return fetch(`${BASE}/clinical-assess/${encodeURIComponent(patientId)}`, {
    headers: kbHeaders(kbName),
  }).then(r => r.json())
}
