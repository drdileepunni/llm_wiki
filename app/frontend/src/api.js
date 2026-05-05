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

export async function sendChat(question, kbName, images = [], mode = 'qna') {
  const res = await fetch(`${BASE}/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ question, images, mode }),
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

export async function deleteWikiFile(path, kbName) {
  const res = await fetch(`${BASE}/wiki/file?path=${encodeURIComponent(path)}`, {
    method: 'DELETE',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
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

export async function getGapIntelligence(kbName) {
  return fetch(`${BASE}/wiki/gap-intelligence`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function createGap(title, missing_sections, kbName) {
  const res = await fetch(`${BASE}/wiki/gaps`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ title, missing_sections }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function updateGap(stem, { title, missing_sections } = {}, kbName) {
  const res = await fetch(`${BASE}/wiki/gaps/${encodeURIComponent(stem)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ title, missing_sections }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// ── Gap resolver ───────────────────────────────────────────────────────────────

export async function deleteGap(gapStem, kbName) {
  const res = await fetch(`${BASE}/resolve/gaps/${encodeURIComponent(gapStem)}`, {
    method: 'DELETE',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

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

export async function verifyGaps(gapStem, kbName) {
  const res = await fetch(`${BASE}/resolve/verify-gaps`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ gap_stem: gapStem || '' }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
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

export async function getWikiActivity(kbName, limit = 500) {
  return fetch(`${BASE}/wiki/activity?limit=${limit}`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function getWikiContamination(kbName) {
  return fetch(`${BASE}/wiki/contamination`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function runDefrag(kbName, path = null, dryRun = false) {
  const params = new URLSearchParams()
  if (path) params.set('path', path)
  if (dryRun) params.set('dry_run', 'true')
  const res = await fetch(`${BASE}/wiki/defrag?${params}`, {
    method: 'POST',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function runMigrateScope(kbName) {
  const res = await fetch(`${BASE}/wiki/migrate/scope`, {
    method: 'POST', headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function runScanContamination(kbName) {
  const res = await fetch(`${BASE}/wiki/migrate/scan-contamination`, {
    method: 'POST', headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function runReconcileGaps(kbName) {
  const res = await fetch(`${BASE}/wiki/migrate/reconcile-gaps`, {
    method: 'POST', headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function markFalsePositive(kbName, path, section, belongs_on) {
  const res = await fetch(`${BASE}/wiki/false-positive`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ path, section, belongs_on }),
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

export async function updateQuestion(sourceSlug, questionId, question, kbName) {
  const res = await fetch(`${BASE}/assess/${encodeURIComponent(sourceSlug)}/questions`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ question_id: questionId, question }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function assessJobStatus(jobId) {
  return fetch(`${BASE}/assess/jobs/${jobId}`).then(r => r.json())
}

// ── Clinical Assessment ────────────────────────────────────────────────────────

export async function listAvailablePatients() {
  return fetch(`${BASE}/clinical-assess/available`).then(r => r.json())
}

export async function listPatientSnapshots(patientId) {
  return fetch(`${BASE}/clinical-assess/patients/${encodeURIComponent(patientId)}/snapshots`).then(r => r.json())
}

export async function runClinicalAssessment(patientId, kbName, model = null, snapshotNum = null, usePatientContext = false, reasoningModel = null, overwriteRunId = null) {
  const res = await fetch(`${BASE}/clinical-assess/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({
      patient_id: patientId,
      model: model || undefined,
      reasoning_model: reasoningModel || undefined,
      snapshot_num: snapshotNum != null ? snapshotNum : undefined,
      use_patient_context: usePatientContext,
      overwrite_run_id: overwriteRunId || undefined,
    }),
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

export async function getClinicalAssessment(patientId, runId, kbName) {
  return fetch(
    `${BASE}/clinical-assess/${encodeURIComponent(patientId)}/${encodeURIComponent(runId)}`,
    { headers: kbHeaders(kbName) }
  ).then(r => r.json())
}

export async function deleteClinicalAssessment(patientId, runId, kbName) {
  const res = await fetch(
    `${BASE}/clinical-assess/${encodeURIComponent(patientId)}/${encodeURIComponent(runId)}`,
    { method: 'DELETE', headers: kbHeaders(kbName) }
  )
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function saveRunComment(patientId, runId, comment, kbName) {
  const res = await fetch(
    `${BASE}/clinical-assess/${encodeURIComponent(patientId)}/${encodeURIComponent(runId)}/comment`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
      body: JSON.stringify({ comment }),
    }
  )
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function rateSnapshotApi(patientId, runId, snapshotNum, { rating, knowledge_gaps } = {}, kbName) {
  const res = await fetch(
    `${BASE}/clinical-assess/${encodeURIComponent(patientId)}/${encodeURIComponent(runId)}/snapshots/${snapshotNum}/rating`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
      body: JSON.stringify({ rating, knowledge_gaps }),
    }
  )
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function generateScenario(description, model = null) {
  const res = await fetch(`${BASE}/clinical-assess/generate-scenario`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description, model: model || undefined }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function runCustomScenario(snapshot, kbName, model = null, reasoningModel = null) {
  const res = await fetch(`${BASE}/clinical-assess/run-custom`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({
      clinical_context: snapshot.clinical_context,
      csv_content: snapshot.csv_content,
      question: snapshot.question,
      phase: snapshot.phase || '',
      difficulty: snapshot.difficulty || '',
      display_name: snapshot.display_name || undefined,
      model: model || undefined,
      reasoning_model: reasoningModel || undefined,
    }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// ── Learn (learning loop) ──────────────────────────────────────────────────────

export async function startLearnRun(cpmrn, encounter, kbName, numSnapshots = 2, reviewQuestions = true) {
  const res = await fetch(`${BASE}/learn/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ cpmrn, encounter, num_snapshots: numSnapshots, review_questions: reviewQuestions }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function learnJobStatus(runId, kbName) {
  return fetch(`${BASE}/learn/jobs/${runId}`, {
    headers: kbHeaders(kbName),
  }).then(r => r.json())
}

export async function cancelLearnRun(runId) {
  const res = await fetch(`${BASE}/learn/jobs/${runId}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function resumeLearnRun(runId, questions, kbName) {
  const res = await fetch(`${BASE}/learn/jobs/${runId}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ questions }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function deleteLearnRun(runId, kbName) {
  const res = await fetch(`${BASE}/learn/jobs/${runId}`, { method: 'DELETE', headers: kbHeaders(kbName) })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function restartLearnRun(runId, kbName) {
  const res = await fetch(`${BASE}/learn/jobs/${runId}/restart`, {
    method: 'POST',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function listLearnRuns(kbName) {
  return fetch(`${BASE}/learn/`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

// ── Order generation ───────────────────────────────────────────────────────────

// ── MedGemma VM control ────────────────────────────────────────────────────────

export async function getVMStatus() {
  return fetch(`${BASE}/vm/status`).then(r => r.json())
}

export async function startVM() {
  const res = await fetch(`${BASE}/vm/start`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function stopVM() {
  const res = await fetch(`${BASE}/vm/stop`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getCPUVMStatus() {
  return fetch(`${BASE}/vm/cpu/status`).then(r => r.json())
}

export async function startCPUVM() {
  const res = await fetch(`${BASE}/vm/cpu/start`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function stopCPUVM() {
  const res = await fetch(`${BASE}/vm/cpu/stop`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getActiveInstance() {
  return fetch(`${BASE}/vm/active`).then(r => r.json())
}

export async function setActiveInstance(instance) {
  const res = await fetch(`${BASE}/vm/active`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instance }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

// ── Log capture ────────────────────────────────────────────────────────────────

export async function startLogCapture() {
  const res = await fetch(`${BASE}/logs/capture/start`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function stopLogCapture() {
  const res = await fetch(`${BASE}/logs/capture/stop`, { method: 'POST' })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getLogCaptureStatus() {
  return fetch(`${BASE}/logs/capture/status`).then(r => r.json())
}

export async function listLogFiles() {
  return fetch(`${BASE}/logs/files`).then(r => r.json())
}

// ── Viva (teacher-student loop) ────────────────────────────────────────────────

export async function getVivaPatient() {
  return fetch(`${BASE}/viva/patient`).then(r => r.json())
}

export async function getVivaPatientLiveState() {
  const res = await fetch(`${BASE}/viva/patient/live-state`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function createVivaPatient(details) {
  const res = await fetch(`${BASE}/viva/patient`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(details),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function placeVivaOrder(order) {
  const res = await fetch(`${BASE}/viva/patient/place-order`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(order),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function startViva(topic, maxTurns = 8, model = null, kbName) {
  const res = await fetch(`${BASE}/viva/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ topic, max_turns: maxTurns, model: model || undefined }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function runVivaTurn(sessionId, model = null, kbName) {
  const res = await fetch(`${BASE}/viva/${encodeURIComponent(sessionId)}/turn`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ model: model || undefined }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function listVivaSessions(kbName) {
  return fetch(`${BASE}/viva/`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function getVivaSession(sessionId, kbName) {
  return fetch(`${BASE}/viva/${encodeURIComponent(sessionId)}`, {
    headers: kbHeaders(kbName),
  }).then(r => r.json())
}

export async function forkVivaSession(sessionId, kbName) {
  const res = await fetch(`${BASE}/viva/${encodeURIComponent(sessionId)}/fork`, {
    method: 'POST',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function deleteVivaSession(sessionId, kbName) {
  const res = await fetch(`${BASE}/viva/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    headers: kbHeaders(kbName),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function rerunVivaTurn(sessionId, turnNum, model = null, kbName) {
  const res = await fetch(`${BASE}/viva/${encodeURIComponent(sessionId)}/rerun-turn/${turnNum}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({ model }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getVivaProvenance(orderRunId) {
  const res = await fetch(`${BASE}/viva/provenance?order_run_id=${encodeURIComponent(orderRunId)}`)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function getGraphData(kbName) {
  return fetch(`${BASE}/graph/data`, { headers: kbHeaders(kbName) }).then(r => r.json())
}

export async function generateOrders({ recommendations, cpmrn, patientType, model }, kbName) {
  const res = await fetch(`${BASE}/orders/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...kbHeaders(kbName) },
    body: JSON.stringify({
      recommendations,
      cpmrn: cpmrn || null,
      patient_type: patientType || 'adult',
      model: model || null,
    }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}
