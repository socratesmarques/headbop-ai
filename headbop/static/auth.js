/* Session tokens stay in HttpOnly cookies. Only the CSRF nonce reaches JavaScript. */
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const i18n = window.headbopI18n;
  let csrf = null;
  let user = null;
  let projectsRequest = 0;
  let sessionTimer;
  const timeoutError = () => ({ key: "error.request_timeout", params: {} });
  function showMessage(id, message) {
    const node = el(id);
    node.hidden = !message;
    if (message) i18n.bindText(node, message.key, message.params);
    else { i18n.unbind(node); node.textContent = ""; }
  }
  async function request(url, options = {}) {
    const controller = new AbortController();
    const headers = new Headers(options.headers);
    if (options.method && options.method !== "GET" && csrf) headers.set("X-CSRF-Token", csrf);
    let timer;
    try {
      return await Promise.race([
        new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(timeoutError()); }, 15000); }),
        (async () => {
          const response = await fetch(url, { ...options, headers, signal: controller.signal, credentials: "same-origin" });
          if (response.status === 204) return null;
          let payload;
          try { payload = await response.json(); }
          catch (_) { throw { key: "error.network", params: {} }; }
          if (!response.ok) {
            if (response.status === 401 && url !== "/api/auth/login") expired();
            throw i18n.errorMessage(payload);
          }
          return payload;
        })(),
      ]);
    } catch (error) {
      throw error.key ? error : { key: "error.network", params: {} };
    } finally { clearTimeout(timer); }
  }
  function signedOut(message) {
    clearTimeout(sessionTimer);
    csrf = null; user = null; projectsRequest++;
    el("studio").hidden = true;
    el("account-panel").hidden = true;
    el("auth-panel").hidden = false;
    el("import-button").hidden = true;
    const video = el("preview-video");
    video.pause(); video.removeAttribute("src"); video.load();
    showMessage("auth-notice", message);
  }
  function expired() {
    signedOut({ key: "auth.sessionExpired", params: {} });
    // A subsequent login reloads the document before exposing the new account.
    // That clears every canvas, object URL and pending app operation.
    el("login-username").focus();
  }
  async function refreshProjects() {
    if (!user) return;
    const generation = ++projectsRequest;
    try {
      const payload = await request("/api/projects");
      if (generation !== projectsRequest || !user) return;
      const select = el("project-history");
      const previous = select.value;
      i18n.unbind(select);
      const placeholder = document.createElement("option");
      placeholder.value = "";
      i18n.bindText(placeholder, payload.projects.length ? "auth.chooseProject" : "auth.noProjects");
      select.replaceChildren(placeholder);
      for (const project of payload.projects) {
        const option = document.createElement("option");
        option.value = project.id;
        i18n.bindText(option, "auth.projectLabel", {
          id: project.id.slice(0, 8), width: project.width, height: project.height, duration: Number(project.duration).toFixed(1),
        });
        select.append(option);
      }
      select.value = [...select.options].some((option) => option.value === previous) ? previous : "";
      select.disabled = !payload.projects.length;
      el("open-project-button").disabled = !select.value;
      showMessage("account-notice", null);
    } catch (error) { if (user) showMessage("account-notice", error); }
  }
  function signedIn(session) {
    user = session.user; csrf = session.csrf;
    el("studio").hidden = false;
    el("account-panel").hidden = false;
    el("auth-panel").hidden = true;
    el("import-button").hidden = false;
    i18n.bindText(el("account-user"), "auth.signedInAs", { name: user.username });
    clearTimeout(sessionTimer);
    sessionTimer = setTimeout(expired, Math.max(0, session.expires_at * 1000 - Date.now()));
    refreshProjects();
  }
  window.headbopAuth = { get csrf() { return csrf; }, expired, refreshProjects };
  document.querySelectorAll("[data-auth-text]").forEach((node) => i18n.bindText(node, node.dataset.authText));
  el("import-button").hidden = true;
  el("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = el("login-button");
    if (button.disabled) return;
    button.disabled = true;
    i18n.bindText(button, "auth.signingIn");
    showMessage("auth-notice", null);
    try {
      await request("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: el("login-username").value, password: el("login-password").value }),
      });
      el("login-password").value = "";
      window.location.reload();
    } catch (error) { showMessage("auth-notice", error); }
    finally { el("login-password").value = ""; button.disabled = false; i18n.bindText(button, "auth.signIn"); }
  });
  el("logout-button").addEventListener("click", async () => {
    const button = el("logout-button"); button.disabled = true;
    try { await request("/api/auth/logout", { method: "POST" }); window.location.reload(); }
    catch (error) { showMessage("account-notice", error); }
    finally { button.disabled = false; }
  });
  el("project-history").addEventListener("change", () => { el("open-project-button").disabled = !el("project-history").value; });
  el("project-history-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const id = el("project-history").value;
    if (id && user) document.dispatchEvent(new CustomEvent("headbop:open-project", { detail: { id } }));
  });
  request("/api/auth/session").then(signedIn).catch((error) => {
    signedOut(error.key === "error.auth_required" ? null : error);
  });
})();
