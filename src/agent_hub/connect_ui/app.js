const providerOrder = ["claude", "grok", "gemini", "gpt"];
const providerMarks = { claude: "AI", grok: "G", gemini: "✦", gpt: "GPT" };

function bootstrapSession() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const supplied = fragment.get("session");
  if (supplied) {
    window.sessionStorage.setItem("agent_hub_session", supplied);
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}`,
    );
  }
  return window.sessionStorage.getItem("agent_hub_session") || "";
}

const sessionToken = bootstrapSession();
const state = {
  providers: {},
  summary: {},
  selected: "claude",
  inlineConsent: false,
  jobs: {},
  pollTimers: {},
  pollGenerations: {},
  pollFailures: {},
  busy: {},
  pendingConsentAction: null,
  consentProvider: null,
  forgetProvider: null,
  forgetInFlight: null,
  statusGeneration: 0,
  modelCatalogs: {},
  modelSelections: {},
  modelErrors: {},
  modelNotices: {},
  modelGenerations: {},
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusView(provider) {
  if (provider.ready) {
    return { label: "연결됨", tone: "ready", message: "Agent Hub에서 사용할 준비가 완료되었습니다." };
  }
  if (provider.authenticated && !provider.consent) {
    return {
      label: "동의 필요",
      tone: "attention",
      message: "로그인은 확인됐지만 Agent Hub 사용 동의가 필요합니다.",
    };
  }
  if (provider.consent && !provider.authenticated) {
    return {
      label: "로그인 필요",
      tone: "attention",
      message: "사용 동의가 완료되었습니다. 공식 계정 로그인을 진행해 주세요.",
    };
  }
  if (provider.consent && provider.authenticated) {
    return {
      label: "확인 필요",
      tone: "attention",
      message: warningMessage(provider.warnings?.[0]),
    };
  }
  return {
    label: "연결 안 됨",
    tone: "idle",
    message: "사용 동의와 계정 로그인이 필요합니다.",
  };
}

function warningMessage(code) {
  const messages = {
    auth_refresh_required: "로그인 갱신이 필요합니다. 다시 로그인한 뒤 연결을 확인해 주세요.",
    credentials_missing: "로그인 정보가 없습니다. 계정 로그인을 진행해 주세요.",
    provider_not_configured: "로그인은 확인됐지만 provider 구성을 완료하지 못했습니다.",
    provider_not_ready: "로그인은 확인됐지만 provider가 아직 준비되지 않았습니다.",
    codex_api_key_mode_not_subscription:
      "Codex가 API key 모드입니다. ChatGPT 구독 계정으로 다시 로그인해 주세요.",
    codex_app_server_unavailable:
      "공식 Codex 상태 API 대신 CLI 로그인 상태로 확인했습니다.",
    codex_protocol_error: "공식 Codex 로그인 상태를 확인하지 못했습니다.",
  };
  return messages[code] || "로그인은 확인됐지만 연결 상태를 추가로 확인해야 합니다.";
}

function currentJob(provider = state.selected) {
  return state.jobs[provider] || null;
}

function isBusy(provider, kind) {
  return Boolean(state.busy[`${provider}:${kind}`]);
}

function setBusy(provider, kind, value) {
  const key = `${provider}:${kind}`;
  if (value) state.busy[key] = true;
  else delete state.busy[key];
}

function providerAuthFingerprint(provider) {
  return [
    Boolean(provider?.consent),
    Boolean(provider?.configured),
    Boolean(provider?.authenticated),
    Boolean(provider?.ready),
    provider?.auth_mode || "",
  ].join(":");
}

function clearModelCatalog(provider) {
  state.modelGenerations[provider] = (state.modelGenerations[provider] || 0) + 1;
  delete state.modelCatalogs[provider];
  delete state.modelSelections[provider];
  delete state.modelErrors[provider];
  delete state.modelNotices[provider];
  setBusy(provider, "models", false);
}

function iconCheck(complete) {
  if (complete) {
    return `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="m6.5 12.5 3.4 3.4L18 7.8"></path>
      </svg>`;
  }
  return "";
}

async function request(path, options = {}) {
  const config = {
    method: options.method || "GET",
    headers: {
      "X-Agent-Hub-Intent": "provider-management",
      "X-Agent-Hub-Session": sessionToken,
    },
  };
  if (options.body !== undefined) {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, config);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload?.success === false) {
    const error = new Error(
      payload?.error?.message || payload?.text || "요청을 완료하지 못했습니다.",
    );
    error.code = payload?.error?.code;
    throw error;
  }
  return payload;
}

async function loadStatus({ quiet = false } = {}) {
  const generation = state.statusGeneration + 1;
  state.statusGeneration = generation;
  if (!quiet) {
    $("#refresh-button").classList.add("is-loading");
  }
  try {
    const payload = await request("/api/status");
    if (state.statusGeneration !== generation) return false;
    const previousProviders = state.providers;
    state.providers = payload.providers;
    state.summary = payload.summary;
    for (const [providerId, provider] of Object.entries(state.providers)) {
      const previous = previousProviders[providerId];
      if (
        previous &&
        providerAuthFingerprint(previous) !== providerAuthFingerprint(provider)
      ) {
        clearModelCatalog(providerId);
      }
    }
    if (!state.providers[state.selected]) {
      state.selected = providerOrder.find((id) => state.providers[id]) || "claude";
    }
    render();
    return true;
  } catch (error) {
    if (state.statusGeneration !== generation) return false;
    $("#detail-panel").innerHTML = `
      <div class="error-state">
        <div>
          <h2>연결 상태를 불러오지 못했습니다.</h2>
          <p>${escapeHtml(error.message)}</p>
          <button class="secondary-button" data-action="retry-status" type="button">다시 시도</button>
        </div>
      </div>`;
    showToast(error.message);
    return false;
  } finally {
    if (state.statusGeneration === generation) {
      $("#refresh-button").classList.remove("is-loading");
    }
  }
}

function render() {
  renderSummary();
  renderProviderList();
  renderDetail();
}

function renderSummary() {
  const total = state.summary.total ?? 0;
  $("#summary-ready").textContent = `${state.summary.ready ?? 0} / ${total}`;
  $("#summary-auth").textContent = String(state.summary.authenticated ?? 0);
  $("#summary-consent").textContent = String(state.summary.consent_required ?? 0);
}

function renderProviderList() {
  $("#provider-list").innerHTML = providerOrder
    .filter((id) => state.providers[id])
    .map((id) => {
      const provider = state.providers[id];
      const view = statusView(provider);
      return `
        <button class="provider-row" type="button" data-provider="${id}"
          aria-current="${state.selected === id}">
          <span class="provider-icon ${id}">${escapeHtml(providerMarks[id])}</span>
          <span class="provider-copy">
            <strong>${escapeHtml(provider.label)}</strong>
            <span title="${escapeHtml(provider.default_model)}">${escapeHtml(provider.default_model)}</span>
          </span>
          <span class="provider-status" data-tone="${view.tone}">
            <i></i>${escapeHtml(view.label)}
          </span>
        </button>`;
    })
    .join("");
}

function renderDetail() {
  const provider = state.providers[state.selected];
  if (!provider) return;
  const view = statusView(provider);
  const consentComplete = provider.consent;
  const authComplete = provider.authenticated;
  const ready = provider.ready;
  const loginBusy = isBusy(provider.id, "login");
  const testBusy = isBusy(provider.id, "test");
  const forgetBusy = Boolean(state.forgetInFlight);
  const job = currentJob(provider.id);
  const jobBusy = job && ["pending", "working", "waiting"].includes(job.state);
  const primary = !consentComplete
    ? "동의하고 연결"
    : !authComplete
      ? "로그인 시작"
      : "연결 테스트";
  const testCopy = ready
    ? "모델 목록을 안전하게 조회해 실제 연결을 확인할 수 있습니다."
    : "동의와 로그인을 완료하면 안전한 상태 검사를 실행합니다.";

  $("#detail-panel").innerHTML = `
    <div class="detail-header">
      <h2>${escapeHtml(provider.label)} 연결</h2>
      <p class="detail-state" data-tone="${view.tone}">
        <i class="status-dot"></i>${escapeHtml(view.message)}
      </p>
    </div>
    <div class="setup-flow">
      <section class="setup-step">
        <span class="step-number ${consentComplete ? "complete" : "active"}">
          ${consentComplete ? iconCheck(true) : "1"}
        </span>
        <div class="step-copy">
          <h3>사용 동의</h3>
          <p>Agent Hub가 현재 ${escapeHtml(provider.session_label)}을 사용하도록 허용합니다.</p>
          ${
            consentComplete
              ? `<p class="success-copy">로컬 사용 동의가 확인되었습니다.</p>`
              : `<label class="inline-consent">
                  <input type="checkbox" id="inline-consent-check" ${state.inlineConsent ? "checked" : ""}>
                  <span>로컬 구독 세션 사용에 동의합니다.</span>
                </label>`
          }
        </div>
        <button class="text-button" type="button" data-action="show-consent"
          ${loginBusy || testBusy || forgetBusy || jobBusy ? "disabled" : ""}>자세한 범위 보기</button>
      </section>
      <section class="setup-step">
        <span class="step-number ${authComplete ? "complete" : consentComplete ? "active" : ""}">
          ${authComplete ? iconCheck(true) : "2"}
        </span>
        <div class="step-copy">
          <h3>계정 로그인</h3>
          <p class="${authComplete ? "success-copy" : ""}">
            ${
              authComplete
                ? `${escapeHtml(provider.login_owner)} 로그인이 확인되었습니다.`
                : `${escapeHtml(provider.login_owner)}의 공식 로그인 화면을 사용합니다.`
            }
          </p>
        </div>
        <button class="secondary-button" type="button" data-action="login"
          ${loginBusy || forgetBusy || jobBusy ? "disabled" : ""}>
          ${loginBusy ? "시작 중…" : authComplete ? "다시 로그인" : "로그인"}
        </button>
      </section>
      <section class="setup-step">
        <span class="step-number ${ready ? "complete" : consentComplete && authComplete ? "active" : ""}">
          ${ready ? iconCheck(true) : "3"}
        </span>
        <div class="step-copy">
          <h3>연결 확인</h3>
          <p>${escapeHtml(testCopy)}</p>
        </div>
        <button class="secondary-button" type="button" data-action="test"
          ${
            consentComplete &&
            authComplete &&
            !loginBusy &&
            !testBusy &&
            !forgetBusy &&
            !jobBusy
              ? ""
              : "disabled"
          }>
          ${testBusy ? "확인 중…" : "연결 테스트"}
        </button>
      </section>
    </div>
    ${renderJobPanel()}
    ${renderModelSettings(
      provider,
      Boolean(loginBusy || testBusy || forgetBusy || jobBusy),
    )}
    <div class="detail-actions">
      <button class="primary-button" type="button" data-action="primary"
        ${
          (!consentComplete && !state.inlineConsent) ||
          loginBusy ||
          testBusy ||
          forgetBusy ||
          jobBusy
            ? "disabled"
            : ""
        }>
        ${escapeHtml(primary)}
      </button>
      ${
        consentComplete
          ? `<button class="text-button" type="button" data-action="disconnect"
              ${loginBusy || testBusy || forgetBusy || jobBusy ? "disabled" : ""}>연결 해제</button>`
          : ""
      }
      ${
        provider.supports_local_logout &&
        (provider.local_credentials_present || provider.pending_login_present)
          ? `<button class="text-button" type="button" data-action="forget-local"
              ${loginBusy || testBusy || forgetBusy || jobBusy ? "disabled" : ""}>${
                forgetBusy
                  ? "삭제 중…"
                  : provider.local_credentials_present
                  ? "로컬 로그인 정보 삭제"
                  : "기존 로그인 취소"
              }</button>`
          : ""
      }
    </div>`;
}

function modelSourceLabel(provider) {
  const labels = {
    saved: "Agent Hub 저장값",
    saved_chat: "Agent Hub 대화 설정",
    environment: "환경 설정",
    profile: "활성 프로필",
    provider_default: "Agent Hub 기본값",
  };
  return labels[provider.model_source] || "현재 기본값";
}

function renderModelSettings(provider, jobBusy) {
  const catalog = state.modelCatalogs[provider.id];
  const selection =
    state.modelSelections[provider.id] || catalog?.selected_model || provider.default_model;
  const loading = isBusy(provider.id, "models");
  const saving = isBusy(provider.id, "model-save");
  const resetting = isBusy(provider.id, "model-reset");
  const settingsBlocked = Boolean(provider.settings_error);
  const selectedItem = catalog?.models?.find((item) => item.id === selection);
  const canSave = Boolean(
    catalog &&
      selectedItem?.selectable &&
      selection !== provider.default_model &&
      !loading &&
      !saving &&
      !resetting &&
      !settingsBlocked &&
      !jobBusy,
  );
  const source = catalog?.source || "local";
  const catalogCopy = catalog
    ? catalog.refreshed
      ? "연결된 provider에서 최신 목록을 확인했습니다."
      : catalog.live_unavailable
        ? provider.ready
          ? "최신 목록을 확인하지 못해 로컬 안전 목록을 표시합니다. 잠시 후 다시 시도해 주세요."
          : "로그인이 완료되지 않아 로컬 안전 목록을 표시합니다."
        : "빠른 로컬 목록입니다. 연결 후 최신 목록을 새로고칠 수 있습니다."
    : provider.ready
      ? "연결된 provider에서 현재 계정의 최신 모델 목록을 불러옵니다."
      : "로그인 전에는 로컬 안전 목록에서 미리 선택할 수 있습니다.";
  const options = (catalog?.models || [])
    .map((item) => {
      const unavailable = item.selectable ? "" : " (현재 catalog에서 확인되지 않음)";
      return `<option value="${escapeHtml(item.id)}"
        ${item.id === selection ? "selected" : ""}
        ${item.selectable ? "" : "disabled"}>
        ${escapeHtml(item.display)}${escapeHtml(unavailable)}
      </option>`;
    })
    .join("");
  return `
    <section class="model-settings" aria-labelledby="model-settings-title">
      <div class="model-settings-header">
        <div>
          <h3 id="model-settings-title">Agent Hub 기본 텍스트 모델</h3>
          <p>이 provider로 보내는 기본 대화·문서 작업에 적용합니다. 작업에서 모델을 직접 지정하면 그 값이 우선합니다.</p>
        </div>
        <span class="model-source">${escapeHtml(modelSourceLabel(provider))}</span>
      </div>
      <div class="model-current">
        <span>현재 적용</span>
        <code title="${escapeHtml(provider.default_model)}">${escapeHtml(provider.default_model)}</code>
      </div>
      <p class="model-help" id="model-help-${provider.id}">${escapeHtml(catalogCopy)}</p>
      ${
        settingsBlocked
          ? `<p class="form-error model-message" role="alert">
              로컬 모델 설정 파일을 읽을 수 없습니다. 파일을 복구한 뒤 다시 시도해 주세요.
            </p>`
          : ""
      }
      ${
        catalog
          ? `<label class="model-label" for="model-select-${provider.id}">텍스트 모델 선택</label>
            <div class="model-control-row">
              <select id="model-select-${provider.id}" data-model-select="${provider.id}"
                aria-describedby="model-help-${provider.id}"
                ${loading || saving || resetting || settingsBlocked || jobBusy ? "disabled" : ""}>
                ${options}
              </select>
              <button class="secondary-button" type="button" data-action="save-model"
                ${canSave ? "" : "disabled"}>
                ${saving ? "저장 중…" : "선택 저장"}
              </button>
            </div>
            <div class="model-secondary-actions">
              <button class="text-button" type="button" data-action="refresh-models"
                ${provider.ready && !loading && !saving && !resetting && !jobBusy ? "" : "disabled"}>
                ${loading ? "불러오는 중…" : "최신 목록 새로고침"}
              </button>
              ${
                provider.model_overridden
                  ? `<button class="text-button" type="button" data-action="reset-model"
                      ${loading || saving || resetting || settingsBlocked || jobBusy ? "disabled" : ""}>
                      ${resetting ? "지우는 중…" : "Agent Hub 저장값 지우기"}
                    </button>`
                  : ""
              }
            </div>
            <span class="catalog-source">목록 출처: ${escapeHtml(source)}</span>`
          : `<button class="secondary-button model-load-button" type="button"
              data-action="load-models" ${loading || jobBusy ? "disabled" : ""}>
              ${
                loading
                  ? "불러오는 중…"
                  : provider.ready
                    ? "최신 모델 목록 불러오기"
                    : "로컬 모델 목록 불러오기"
              }
            </button>`
      }
      <p class="form-error model-message" role="alert"
        ${state.modelErrors[provider.id] ? "" : "hidden"}>
        ${escapeHtml(state.modelErrors[provider.id] || "")}
      </p>
      <p class="model-message success-copy" role="status"
        ${state.modelNotices[provider.id] ? "" : "hidden"}>
        ${escapeHtml(state.modelNotices[provider.id] || "")}
      </p>
    </section>`;
}

function renderJobPanel() {
  const job = currentJob();
  if (!job) return "";
  const busy = ["pending", "working", "waiting"].includes(job.state);
  return `
    <div class="job-panel" data-state="${escapeHtml(job.state)}">
      ${busy ? '<span class="spinner" aria-hidden="true"></span>' : ""}
      <div>
        <strong>${escapeHtml(job.message)}</strong>
        ${job.user_code ? `<span class="device-code">${escapeHtml(job.user_code)}</span>` : ""}
        ${
          job.action_url
            ? `<a class="login-link" href="${escapeHtml(job.action_url)}"
                target="_blank" rel="noopener noreferrer">로그인 페이지 열기</a>`
            : ""
        }
        ${
          job.fallback_command && job.state !== "complete"
            ? `<span class="fallback-command">터미널에서 직접 실행:
                <code>${escapeHtml(job.fallback_command)}</code></span>`
            : ""
        }
        ${
          job.requires_code && job.state === "waiting"
            ? `<form class="callback-form" id="callback-form">
                <input name="code_or_url" aria-label="Google 리디렉션 URL 또는 인증 코드"
                  placeholder="리디렉션 URL 또는 인증 코드">
                <button class="secondary-button" type="submit">로그인 완료</button>
              </form>`
            : ""
        }
      </div>
    </div>`;
}

async function loadModels({ provider = state.selected, refresh = false } = {}) {
  if (isBusy(provider, "models")) return;
  const generation = (state.modelGenerations[provider] || 0) + 1;
  state.modelGenerations[provider] = generation;
  setBusy(provider, "models", true);
  delete state.modelErrors[provider];
  delete state.modelNotices[provider];
  if (state.selected === provider) renderDetail();
  try {
    const catalog = await request(
      `/api/providers/${provider}/models?refresh=${refresh ? "1" : "0"}`,
    );
    if (state.modelGenerations[provider] !== generation) return;
    state.modelCatalogs[provider] = catalog;
    const previous = state.modelSelections[provider];
    const current =
      catalog.models.find(
        (item) => item.id === previous && item.selectable,
      ) ||
      catalog.models.find((item) => item.id === catalog.selected_model) ||
      catalog.models.find((item) => item.selectable);
    state.modelSelections[provider] = current?.id || "";
    state.modelNotices[provider] = catalog.live_unavailable
      ? state.providers[provider]?.ready
        ? "최신 목록을 확인하지 못해 로컬 안전 목록을 표시했습니다."
        : "로그인 전이라 로컬 안전 목록을 표시했습니다."
      : catalog.refreshed
        ? "최신 모델 목록을 불러왔습니다."
        : "로컬 모델 목록을 불러왔습니다.";
  } catch (error) {
    if (state.modelGenerations[provider] !== generation) return;
    state.modelErrors[provider] = error.message;
  } finally {
    if (state.modelGenerations[provider] === generation) {
      setBusy(provider, "models", false);
      if (state.selected === provider) renderDetail();
    }
  }
}

async function saveModel() {
  const provider = state.selected;
  const catalog = state.modelCatalogs[provider];
  const model = state.modelSelections[provider];
  if (!catalog || !model || isBusy(provider, "model-save")) return;
  setBusy(provider, "model-save", true);
  delete state.modelErrors[provider];
  delete state.modelNotices[provider];
  renderDetail();
  try {
    const result = await request(`/api/providers/${provider}/model`, {
      method: "POST",
      body: {
        model,
        catalog_revision: catalog.catalog_revision,
      },
    });
    await loadStatus({ quiet: true });
    state.modelSelections[provider] = result.selected_model;
    state.modelNotices[provider] =
      `${state.providers[provider].label} 기본 텍스트 모델을 저장했습니다.`;
  } catch (error) {
    state.modelErrors[provider] = error.message;
    if (error.code === "model_catalog_stale") {
      delete state.modelCatalogs[provider];
    }
  } finally {
    setBusy(provider, "model-save", false);
    if (state.selected === provider) renderDetail();
  }
}

async function resetModel() {
  const provider = state.selected;
  if (isBusy(provider, "model-reset")) return;
  setBusy(provider, "model-reset", true);
  delete state.modelErrors[provider];
  delete state.modelNotices[provider];
  renderDetail();
  try {
    const result = await request(`/api/providers/${provider}/model-reset`, {
      method: "POST",
      body: { confirmation: `reset-model:${provider}` },
    });
    await loadStatus({ quiet: true });
    state.modelSelections[provider] = result.selected_model;
    delete state.modelCatalogs[provider];
    state.modelNotices[provider] = result.model_overridden
      ? `${state.providers[provider].label}의 상위 Agent Hub 모델 설정이 계속 적용됩니다.`
      : result.model_source === "environment"
        ? "Agent Hub 저장값을 지웠습니다. 환경 설정 모델이 적용됩니다."
        : "Agent Hub 저장값을 지웠습니다.";
  } catch (error) {
    state.modelErrors[provider] = error.message;
  } finally {
    setBusy(provider, "model-reset", false);
    if (state.selected === provider) renderDetail();
  }
}

function openConsentDialog(continuation = "status") {
  const provider = state.providers[state.selected];
  state.pendingConsentAction = continuation;
  state.consentProvider = provider.id;
  $("#consent-title").textContent = `${provider.label} 사용 동의`;
  $("#consent-copy").textContent =
    `Agent Hub가 ${provider.session_label}을 사용해 모델 요청을 보낼 수 있도록 허용합니다.`;
  $("#consent-capabilities").textContent =
    provider.capabilities.slice(0, 5).join(", ") || "대화, 비교, 코드 검토, 문서 작성";
  const pluginOwned = provider.supports_local_logout;
  $("#consent-storage-copy").textContent = pluginOwned
    ? "동의 여부와 provider OAuth 세션을 이 기기에 저장"
    : "동의 여부와 redacted 연결 상태만 로컬에서 확인";
  $("#consent-owner-copy").textContent = pluginOwned
    ? `로그인과 토큰 갱신은 Agent Hub의 ${provider.label} adapter가 관리합니다.`
    : `로그인과 계정 세션은 ${provider.login_owner}가 계속 관리합니다.`;
  $("#modal-consent-label").textContent =
    `위 내용을 이해했으며 Agent Hub의 ${provider.label} 세션 사용에 동의합니다.`;
  $("#modal-consent-check").checked = state.inlineConsent;
  $("#modal-consent-submit").disabled = !state.inlineConsent;
  $("#modal-consent-submit").textContent =
    continuation === "login" ? "동의하고 로그인" : "동의하고 연결";
  $("#consent-error").textContent = "";
  $("#consent-error").hidden = true;
  $("#consent-dialog").showModal();
}

function reserveLoginWindow(providerId) {
  const provider = state.providers[providerId];
  if (!provider || provider.login_transport !== "browser") return null;
  const loginWindow = window.open("about:blank", "_blank");
  if (!loginWindow) return null;
  try {
    loginWindow.opener = null;
    loginWindow.document.title = `${provider.label} 로그인 준비 중`;
    loginWindow.document.body.textContent = "안전한 로그인 페이지를 준비하고 있습니다.";
  } catch (_error) {
    // The persistent in-app link remains available if the placeholder is inaccessible.
  }
  return loginWindow;
}

function closeLoginWindow(loginWindow) {
  if (!loginWindow || loginWindow.closed) return;
  try {
    loginWindow.close();
  } catch (_error) {
    // A cross-origin window can refuse scripted close; the login link still remains safe.
  }
}

function navigateLoginWindow(loginWindow, url) {
  if (!loginWindow || loginWindow.closed || !url) return false;
  try {
    loginWindow.location.replace(url);
    return true;
  } catch (_error) {
    return false;
  }
}

async function grantConsent(loginWindow = null) {
  const provider = state.consentProvider || state.selected;
  const continuation = state.pendingConsentAction;
  const submit = $("#modal-consent-submit");
  setBusy(provider, "consent", true);
  const cancelButtons = [
    ...$("#consent-dialog").querySelectorAll('button[value="cancel"]'),
  ];
  cancelButtons.forEach((button) => {
    button.disabled = true;
  });
  submit.disabled = true;
  submit.textContent = "저장 중…";
  $("#consent-error").hidden = true;
  try {
    const granted = await request(`/api/providers/${provider}/consent`, {
      method: "POST",
      body: { confirmation: `connect:${provider}` },
    });
    if (granted.consent && state.providers[provider]) {
      state.providers[provider].consent = true;
    }
    clearModelCatalog(provider);
    state.inlineConsent = false;
    await loadStatus({ quiet: true });
    const current = state.providers[provider];
    $("#consent-dialog").close();
    showToast(`${current.label} 사용 동의를 저장했습니다.`);
    if (continuation === "login") {
      await startLogin({ provider, loginWindow });
    } else if (continuation === "test") {
      closeLoginWindow(loginWindow);
      state.selected = provider;
      await startTest();
    } else {
      closeLoginWindow(loginWindow);
    }
  } catch (error) {
    closeLoginWindow(loginWindow);
    $("#consent-error").textContent = error.message;
    $("#consent-error").hidden = false;
    showToast(error.message);
  } finally {
    setBusy(provider, "consent", false);
    cancelButtons.forEach((button) => {
      button.disabled = false;
    });
    submit.disabled = !$("#modal-consent-check").checked;
    submit.textContent =
      continuation === "login" ? "동의하고 로그인" : "동의하고 연결";
  }
}

async function startLogin({ provider = state.selected, loginWindow = null } = {}) {
  const current = state.providers[provider];
  if (!current) return;
  if (!current.consent) {
    state.selected = provider;
    openConsentDialog("login");
    return;
  }
  if (isBusy(provider, "login")) return;
  clearModelCatalog(provider);
  const reservedWindow = loginWindow || reserveLoginWindow(provider);
  setBusy(provider, "login", true);
  if (state.selected === provider) renderDetail();
  try {
    const job = await request(`/api/providers/${provider}/login-start`, {
      method: "POST",
      body: {},
    });
    state.jobs[provider] = job;
    if (state.selected === provider) renderDetail();
    if (job.action_url && !navigateLoginWindow(reservedWindow, job.action_url)) {
      closeLoginWindow(reservedWindow);
      showToast("팝업이 차단됐습니다. 화면의 ‘로그인 페이지 열기’를 눌러 주세요.");
    } else if (!job.action_url) {
      closeLoginWindow(reservedWindow);
    }
    pollJob(provider, job.id);
  } catch (error) {
    closeLoginWindow(reservedWindow);
    showToast(error.message);
  } finally {
    setBusy(provider, "login", false);
    if (state.selected === provider) renderDetail();
  }
}

function clearProviderPoll(provider) {
  clearTimeout(state.pollTimers[provider]);
  delete state.pollTimers[provider];
  state.pollGenerations[provider] = (state.pollGenerations[provider] || 0) + 1;
  state.pollFailures[provider] = 0;
}

function clearProviderJob(provider) {
  clearProviderPoll(provider);
  delete state.jobs[provider];
}

function pollJob(provider, jobId) {
  clearProviderPoll(provider);
  const generation = state.pollGenerations[provider];
  const poll = async () => {
    if (generation !== state.pollGenerations[provider]) return;
    try {
      const job = await request(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (generation !== state.pollGenerations[provider]) return;
      state.jobs[provider] = job;
      state.pollFailures[provider] = 0;
      if (state.selected === provider) renderDetail();
      if (["pending", "working", "waiting"].includes(job.state)) {
        state.pollTimers[provider] = setTimeout(poll, 1400);
      } else {
        if (job.kind === "login") clearModelCatalog(provider);
        showToast(job.message);
        await loadStatus({ quiet: true });
      }
    } catch (error) {
      if (generation !== state.pollGenerations[provider]) return;
      const failures = (state.pollFailures[provider] || 0) + 1;
      state.pollFailures[provider] = failures;
      if (failures <= 3) {
        state.pollTimers[provider] = setTimeout(poll, 900 * failures);
      } else {
        showToast(error.message);
      }
    }
  };
  state.pollTimers[provider] = setTimeout(poll, 700);
}

async function completeLogin(form) {
  const input = new FormData(form).get("code_or_url");
  const provider = state.selected;
  const activeJob = currentJob(provider);
  if (!String(input || "").trim() || !activeJob) return;
  try {
    const job = await request(`/api/providers/${provider}/login-complete`, {
      method: "POST",
      body: { job_id: activeJob.id, code_or_url: input },
    });
    state.jobs[provider] = job;
    if (state.selected === provider) renderDetail();
    pollJob(provider, job.id);
  } catch (error) {
    showToast(error.message);
  }
}

async function startTest() {
  const provider = state.selected;
  if (isBusy(provider, "test")) return;
  setBusy(provider, "test", true);
  renderDetail();
  try {
    const job = await request(`/api/providers/${provider}/test`, {
      method: "POST",
      body: {},
    });
    state.jobs[provider] = job;
    renderDetail();
    pollJob(provider, job.id);
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(provider, "test", false);
    if (state.selected === provider) renderDetail();
  }
}

async function disconnect() {
  const provider = state.selected;
  try {
    await request(`/api/providers/${provider}/disconnect`, {
      method: "POST",
      body: { confirmation: `disconnect:${provider}` },
    });
    $("#disconnect-dialog").close();
    clearProviderJob(provider);
    clearModelCatalog(provider);
    await loadStatus({ quiet: true });
    const effective = state.providers[provider].consent;
    showToast(
      effective
        ? "환경 설정으로 동의가 강제되어 연결이 계속 활성화되어 있습니다."
        : `${state.providers[provider].label}의 Agent Hub 연결을 해제했습니다.`,
    );
  } catch (error) {
    showToast(error.message);
  }
}

async function forgetLocal() {
  const provider = state.forgetProvider || state.selected;
  if (state.forgetInFlight) return;
  const dialogButtons = $("#forget-form").querySelectorAll("button");
  state.forgetInFlight = provider;
  setBusy(provider, "forget", true);
  clearModelCatalog(provider);
  dialogButtons.forEach((button) => {
    button.disabled = true;
  });
  if (state.selected === provider) renderDetail();
  try {
    const result = await request(`/api/providers/${provider}/forget-local`, {
      method: "POST",
      body: { confirmation: `forget-local:${provider}` },
    });
    $("#forget-dialog").close();
    clearProviderJob(provider);
    showToast(
      result.removed
        ? `${state.providers[provider].label}의 로컬 로그인 정보를 삭제했습니다.`
        : "삭제할 Agent Hub 로컬 로그인 정보가 없습니다.",
    );
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy(provider, "forget", false);
    if (state.forgetInFlight === provider) {
      state.forgetInFlight = null;
    }
    if (!$("#forget-dialog").open) {
      state.forgetProvider = null;
    }
    dialogButtons.forEach((button) => {
      button.disabled = false;
    });
    await loadStatus({ quiet: true });
  }
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3600);
}

document.addEventListener("click", async (event) => {
  const providerButton = event.target.closest("[data-provider]");
  if (providerButton) {
    state.selected = providerButton.dataset.provider;
    state.inlineConsent = false;
    state.pendingConsentAction = null;
    render();
    if (window.matchMedia("(max-width: 560px)").matches) {
      requestAnimationFrame(() => {
        $("#detail-panel").scrollIntoView({ block: "start" });
      });
    }
    return;
  }
  const actionButton = event.target.closest("[data-action]");
  if (!actionButton) return;
  const action = actionButton.dataset.action;
  if (action === "show-consent") openConsentDialog("status");
  if (action === "login") await startLogin();
  if (action === "test") await startTest();
  if (action === "load-models") {
    const provider = state.providers[state.selected];
    await loadModels({ refresh: Boolean(provider?.ready) });
  }
  if (action === "refresh-models") await loadModels({ refresh: true });
  if (action === "save-model") await saveModel();
  if (action === "reset-model") await resetModel();
  if (action === "retry-status") await loadStatus();
  if (action === "primary") {
    const provider = state.providers[state.selected];
    if (!provider.consent) {
      openConsentDialog(provider.authenticated ? "test" : "login");
    }
    else if (!provider.authenticated) await startLogin();
    else await startTest();
  }
  if (action === "disconnect") {
    $("#disconnect-copy").textContent =
      `${state.providers[state.selected].label}을 Agent Hub에서 더 이상 사용하지 않습니다.`;
    $("#disconnect-dialog").showModal();
  }
  if (action === "forget-local") {
    if (state.forgetInFlight) return;
    state.forgetProvider = state.selected;
    $("#forget-copy").textContent =
      `${state.providers[state.forgetProvider].label}용으로 Agent Hub가 이 기기에 저장한 로그인 정보를 삭제합니다.`;
    $("#forget-dialog").showModal();
  }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "inline-consent-check") {
    state.inlineConsent = event.target.checked;
    renderDetail();
  }
  if (event.target.id === "modal-consent-check") {
    state.inlineConsent = event.target.checked;
    $("#modal-consent-submit").disabled = !event.target.checked;
  }
  if (event.target.matches?.("[data-model-select]")) {
    const provider = event.target.dataset.modelSelect;
    state.modelSelections[provider] = event.target.value;
    delete state.modelErrors[provider];
    delete state.modelNotices[provider];
    renderDetail();
    requestAnimationFrame(() => {
      $(`#model-select-${provider}`)?.focus();
    });
  }
});

document.addEventListener("submit", async (event) => {
  if (event.target.id === "callback-form") {
    event.preventDefault();
    await completeLogin(event.target);
  }
});

$("#consent-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "default") return;
  event.preventDefault();
  const provider = state.consentProvider || state.selected;
  const loginWindow =
    state.pendingConsentAction === "login" ? reserveLoginWindow(provider) : null;
  await grantConsent(loginWindow);
});

$("#consent-dialog").addEventListener("close", () => {
  state.pendingConsentAction = null;
  state.consentProvider = null;
  $("#consent-error").textContent = "";
  $("#consent-error").hidden = true;
});

$("#consent-dialog").addEventListener("cancel", (event) => {
  const provider = state.consentProvider;
  if (provider && isBusy(provider, "consent")) {
    event.preventDefault();
  }
});

$("#disconnect-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "default") return;
  event.preventDefault();
  await disconnect();
});

$("#forget-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "default") return;
  event.preventDefault();
  await forgetLocal();
});

$("#forget-dialog").addEventListener("cancel", (event) => {
  if (state.forgetInFlight) {
    event.preventDefault();
  }
});

$("#forget-dialog").addEventListener("close", () => {
  if (!state.forgetInFlight) {
    state.forgetProvider = null;
  }
});

$("#refresh-button").addEventListener("click", () => loadStatus());
$("#shutdown-button").addEventListener("click", async () => {
  try {
    Object.keys(state.pollTimers).forEach(clearProviderPoll);
    await request("/api/shutdown", { method: "POST", body: {} });
    document.body.innerHTML = `
      <main class="loading-state">
        <div><h1>Agent Hub 연결 관리를 종료했습니다.</h1><p>이 탭을 닫아도 됩니다.</p></div>
      </main>`;
  } catch (error) {
    showToast(error.message);
  }
});

loadStatus();
