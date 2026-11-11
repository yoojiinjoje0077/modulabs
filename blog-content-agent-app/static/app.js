const form = document.querySelector('#agent-form');
const statusPill = document.querySelector('#status');
const generateButton = document.querySelector('#generate');
const demoButton = document.querySelector('#demo');
const resultSection = document.querySelector('#result-section');
const preview = document.querySelector('#preview');
const jsonOutput = document.querySelector('#json-output');
const toast = document.querySelector('#toast');
const loginButton = document.querySelector('#chatgpt-login');
const addButton = document.querySelector('#chatgpt-add');
const logoutButton = document.querySelector('#chatgpt-logout');
const accountSelect = document.querySelector('#account-select');
const modelSelect = document.querySelector('#model-select');
const authOptions = document.querySelector('#auth-options');
const authNote = document.querySelector('#auth-note');
let latest = null;
let authState = null;
let loginPoll = null;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 4200);
}

function values() {
  const data = Object.fromEntries(new FormData(form).entries());
  data.topic_count = Number(data.topic_count || 10);
  return data;
}

async function loadStatus() {
  try {
    const response = await fetch('/api/status');
    const data = await response.json();
    authState = data.chatgpt;
    const signedIn = Boolean(authState?.signed_in);
    statusPill.classList.toggle('live', signedIn || data.key_state === 'configured_unverified');
    statusPill.classList.toggle('demo', !signedIn && data.key_state !== 'configured_unverified');
    statusPill.querySelector('span:last-child').textContent = signedIn
      ? `ChatGPT 연결 · ${authState.email || '계정'}${authState.model ? ` · ${authState.model}` : ' · 모델 선택 필요'}`
      : data.key_state === 'looks_like_id'
      ? '키 식별자 입력됨 · 비밀 키 필요'
      : data.key_state === 'configured_unverified'
        ? `키 입력됨 · 유효성 미확인 · ${data.model}`
        : 'ChatGPT 로그인 필요';
    const savedAccounts = authState?.accounts || [];
    addButton.hidden = savedAccounts.length === 0;
    logoutButton.hidden = !signedIn;
    authOptions.hidden = savedAccounts.length === 0 && !signedIn;
    modelSelect.parentElement.hidden = !signedIn;
    loginButton.textContent = savedAccounts.length ? '선택 계정으로 로그인' : 'Continue with ChatGPT';
    accountSelect.replaceChildren();
    savedAccounts.forEach(account => {
      const option = new Option(account.email || 'ChatGPT 계정', account.client_id);
      accountSelect.add(option);
    });
    accountSelect.value = authState?.client_id || savedAccounts[0]?.client_id || '';
    authNote.textContent = signedIn
      ? `${authState.email || '계정'} 연결됨 · 사용할 모델을 확인하세요. 사용량은 ChatGPT 설정에서 볼 수 있습니다.`
      : 'API 키 없이 사용하려면 ChatGPT로 로그인하세요. 요금제 사용량과 계정별 모델 제한이 적용됩니다.';
    if (signedIn) await loadModels(authState.model);
  } catch {
    statusPill.querySelector('span:last-child').textContent = '서버 연결 확인 필요';
  }
}

async function postJson(path, body = {}) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '요청에 실패했습니다.');
  return data;
}

async function loadModels(selected) {
  const response = await fetch('/api/models');
  const data = await response.json();
  if (!response.ok) { authNote.textContent = data.error || '모델 목록을 확인할 수 없습니다.'; return; }
  modelSelect.replaceChildren(new Option('모델을 선택해 주세요', ''));
  data.models.forEach(item => modelSelect.add(new Option(item.display_name, item.slug)));
  modelSelect.value = selected || '';
  if (!selected && data.models.some(item => item.slug === 'gpt-6-astra')) {
    await postJson('/api/model', {model: 'gpt-6-astra'});
    modelSelect.value = 'gpt-6-astra';
    authState.model = 'gpt-6-astra';
    statusPill.querySelector('span:last-child').textContent = `ChatGPT 연결 · ${authState.email || '계정'} · gpt-6-astra`;
  } else if (!selected) {
    authNote.textContent = '이 계정에 gpt-6-astra가 없습니다. 목록에서 다른 모델을 직접 선택해야 생성할 수 있습니다.';
  }
}

async function startLogin(clientId = '') {
  const popup = window.open('about:blank', '_blank');
  try {
    const data = await postJson('/api/auth/start', {client_id: clientId});
    if (popup) popup.location.href = data.url;
    else window.location.href = data.url;
    authNote.textContent = '열린 창에서 ChatGPT 로그인을 완료해 주세요.';
    clearInterval(loginPoll);
    let attempts = 0;
    loginPoll = setInterval(async () => {
      if (++attempts > 90) { clearInterval(loginPoll); return; }
      try {
        const status = await (await fetch('/api/status')).json();
        if (status.chatgpt?.signed_in && !status.chatgpt.model) {
          clearInterval(loginPoll);
          await loadStatus();
        }
      } catch { /* Keep waiting for the local server. */ }
    }, 2000);
  } catch (error) {
    if (popup) popup.close();
    showToast(error.message);
  }
}

loginButton.addEventListener('click', () => startLogin(accountSelect.value || ''));
addButton.addEventListener('click', () => startLogin(''));
accountSelect.addEventListener('change', () => startLogin(accountSelect.value));
logoutButton.addEventListener('click', async () => {
  try { await postJson('/api/auth/logout'); await loadStatus(); showToast('ChatGPT 계정 연결을 해제했습니다.'); }
  catch (error) { showToast(error.message); }
});
modelSelect.addEventListener('change', async () => {
  if (!modelSelect.value) return;
  try { await postJson('/api/model', {model: modelSelect.value}); await loadStatus(); }
  catch (error) { showToast(error.message); }
});
window.addEventListener('message', event => {
  if (event.origin === window.location.origin && event.data === 'chatgpt-auth-complete') {
    clearInterval(loginPoll);
    loadStatus();
  }
});

function setBusy(busy, demo) {
  generateButton.disabled = busy;
  demoButton.setAttribute('aria-disabled', String(busy));
  generateButton.innerHTML = busy && !demo ? '<span>◌</span> 기획표와 원고 작성 중…' : '<span>✦</span> 기획표와 원고 한 번에 만들기';
  if (busy && !demo) showToast('기획표 생성 후 첫 번째 주제로 원고를 작성합니다. 시간이 걸릴 수 있습니다.');
}

async function run(demo) {
  if (!form.reportValidity()) return;
  if (!demo && authState?.signed_in && !authState.model) {
    showToast('먼저 사용할 모델을 선택해 주세요.');
    modelSelect.focus();
    return;
  }
  setBusy(true, demo);
  try {
    const response = await fetch(demo ? '/api/demo' : '/api/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(values()),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '요청에 실패했습니다.');
    latest = data;
    preview.srcdoc = data.html;
    jsonOutput.textContent = JSON.stringify(data.result, null, 2);
    document.querySelector('#result-title').textContent = data.mode === 'full' ? '주제별 기획표 + 블로그 원고' : data.mode === 'plan' ? '주제별 기획표' : '블로그 원고';
    document.querySelector('#result-subtitle').textContent = data.demo
      ? '화면 확인용 고정 예시입니다. 입력값을 반영하지 않습니다.'
      : data.mode === 'full'
        ? `첫 번째 주제 「${data.result.selected_topic}」로 톤 ${data.tone}의 원고를 이어서 작성했습니다.`
        : `선택한 톤 ${data.tone}로 생성했습니다. 게시 전에 출처와 수치를 확인해 주세요.`;
    const badge = document.querySelector('#result-badge');
    badge.textContent = data.demo ? '데모 예시' : 'AI 생성 결과';
    badge.classList.toggle('demo', data.demo);
    resultSection.hidden = false;
    selectTab('preview');
    resultSection.scrollIntoView({behavior: 'smooth', block: 'start'});
    showToast(data.demo ? '고정 예시를 열었습니다.' : '원고가 생성되었습니다. 내용을 검토해 주세요.');
  } catch (error) {
    showToast(error.message || '요청을 처리할 수 없습니다.');
  } finally {
    setBusy(false, demo);
  }
}

function selectTab(tab) {
  document.querySelectorAll('.result-tabs button').forEach(button => {
    button.classList.toggle('active', button.dataset.tab === tab);
  });
  document.querySelector('#preview-panel').classList.toggle('hidden', tab !== 'preview');
  document.querySelector('#json-panel').classList.toggle('hidden', tab !== 'json');
}

function download(name, contents, type) {
  const blob = new Blob([contents], {type});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function articleMarkdown(item) {
  return `# ${item.title}\n\n${item.body_markdown}\n\n## 이미지 안내\n${item.image_briefs.map((v, i) => `${i + 1}. ${v}`).join('\n')}\n\n## 출처\n${item.sources.map(v => `- [${v.title}](${v.url})`).join('\n')}\n\n${item.seo_tags.map(v => '#' + v.replace(/^#/, '')).join(' ')}`;
}

function planMarkdown(plan) {
  return plan.topics.map((v, i) => {
    const rows = [['글 주제', v.topic], ['글 제목', v.title], ['SEO 태그', v.seo_tags.join(', ')],
      ['검색자의 질문', v.search_questions], ['글 초안', v.draft], ['필요한 이미지', v.images],
      ['객관 수치·근거', v.metrics], ['독자 효과', v.reader_benefit], ['예상 유입율·유입량', v.traffic]];
    return `## ${String(i + 1).padStart(2, '0')}. ${v.topic}\n\n| 항목 | 내용 |\n|---|---|\n${rows.map(r => `| ${r[0]} | ${String(r[1]).replaceAll('|', '\\|').replaceAll('\n', '<br>')} |`).join('\n')}`;
  }).join('\n\n') + `\n\n${plan.method_note}`;
}

function simpleMarkdown() {
  if (!latest) return '';
  if (latest.mode === 'article') return articleMarkdown(latest.result);
  if (latest.mode === 'plan') return planMarkdown(latest.result);
  return `# 주제별 기획표\n\n${planMarkdown(latest.result.plan)}\n\n---\n\n# 첫 번째 주제의 블로그 원고\n\n${articleMarkdown(latest.result.article)}`;
}

form.addEventListener('submit', event => { event.preventDefault(); run(false); });
demoButton.addEventListener('click', event => { event.preventDefault(); if (demoButton.getAttribute('aria-disabled') !== 'true') run(true); });
document.querySelectorAll('.result-tabs button').forEach(button => {
  button.addEventListener('click', () => selectTab(button.dataset.tab));
});
document.querySelector('#download-html').addEventListener('click', () => {
  if (latest) download(`blog-${latest.mode}${latest.demo ? '-demo' : ''}.html`, latest.html, 'text/html;charset=utf-8');
});
document.querySelector('#download-json').addEventListener('click', () => {
  if (latest) download(`blog-${latest.mode}${latest.demo ? '-demo' : ''}.json`, JSON.stringify(latest.result, null, 2), 'application/json;charset=utf-8');
});
document.querySelector('#download-md').addEventListener('click', () => {
  if (latest) download(`blog-${latest.mode}${latest.demo ? '-demo' : ''}.md`, simpleMarkdown(), 'text/markdown;charset=utf-8');
});
document.querySelector('#evidence-file').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  if (file.size > 120_000) { showToast('파일이 큽니다. 필요한 행만 추려 12,000자 이내로 넣어주세요.'); return; }
  const content = await file.text();
  const field = form.elements.evidence;
  field.value = [field.value, `[파일: ${file.name}]\n${content}`].filter(Boolean).join('\n\n').slice(0, 12_000);
  showToast('파일 내용을 근거 자료 칸에 넣었습니다. 조회 조건을 확인해 주세요.');
});
loadStatus();
