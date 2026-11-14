/* Free browser workspaces: no signup, and private media stays behind an HttpOnly cookie. */
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const i18n = window.headbopI18n;
  let csrf = null;
  let user = null;
  let pendingSession = null;
  let sessionTimer;
  let projectsRequest = 0;
  let routeRequest = 0;
  let mustReload = false;
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
        new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject({ key: "error.request_timeout", params: {} }); }, 15000); }),
        (async () => {
          const response = await fetch(url, { ...options, headers, signal: controller.signal, credentials: "same-origin" });
          if (response.status === 204) return null;
          let payload;
          try { payload = await response.json(); }
          catch (_) { throw { key: "error.network", params: {} }; }
          if (!response.ok) {
            if (response.status === 401) expired();
            throw i18n.errorMessage(payload);
          }
          return payload;
        })(),
      ]);
    } catch (error) {
      throw error?.key ? error : { key: "error.network", params: {} };
    } finally { clearTimeout(timer); }
  }
  function showHome(message = null) {
    routeRequest++;
    projectsRequest++;
    el("home").hidden = false;
    el("studio").hidden = true;
    el("account-panel").hidden = true;
    el("header-start").hidden = false;
    el("back-home").hidden = true;
    el("import-button").hidden = true;
    document.body.classList.add("page-home");
    document.querySelector(".skip-link").setAttribute("href", "#home");
    el("preview-video").pause();
    showMessage("home-notice", message);
  }
  function expired() {
    clearTimeout(sessionTimer);
    csrf = null;
    // A new identity must never inherit canvases, pending work or media from the old page.
    mustReload = true;
    showHome({ key: "guest.expired", params: {} });
    const video = el("preview-video");
    video.removeAttribute("src"); video.load();
    history.replaceState(null, "", "#home");
    el("start-creating").focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }
  async function ensure() {
    if (pendingSession) return pendingSession;
    pendingSession = (async () => {
      const session = await request("/api/auth/guest", { method: "POST" });
      const changed = user && user.id !== session.user.id;
      user = session.user;
      csrf = session.csrf;
      if (mustReload || changed) {
        history.replaceState(null, "", "#studio");
        window.location.reload();
        return null;
      }
      clearTimeout(sessionTimer);
      sessionTimer = setTimeout(expired, Math.max(0, session.expires_at * 1000 - Date.now()));
      return session;
    })();
    try { return await pendingSession; }
    finally { pendingSession = null; }
  }
  async function refreshProjects() {
    if (!user || !csrf) return;
    const generation = ++projectsRequest;
    try {
      const payload = await request("/api/projects");
      if (generation !== projectsRequest || !csrf) return;
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
    } catch (error) { if (csrf) showMessage("account-notice", error); }
  }
  function starting(value) {
    for (const id of ["header-start", "start-creating"]) {
      el(id).setAttribute("aria-disabled", String(value));
      el(id).setAttribute("aria-busy", String(value));
    }
  }
  async function openStudio() {
    const generation = ++routeRequest;
    starting(true);
    showMessage("home-notice", { key: "guest.opening", params: {} });
    try {
      const session = await ensure();
      if (!session || generation !== routeRequest) return;
      el("home").hidden = true;
      el("studio").hidden = false;
      el("account-panel").hidden = false;
      el("header-start").hidden = true;
      el("back-home").hidden = false;
      el("import-button").hidden = false;
      document.body.classList.remove("page-home");
      document.querySelector(".skip-link").setAttribute("href", "#studio");
      showMessage("home-notice", null);
      window.dispatchEvent(new Event("resize"));
      window.scrollTo({ top: 0 });
      refreshProjects();
    } catch (error) {
      if (generation === routeRequest) {
        showHome(error);
        history.replaceState(null, "", "#home");
      }
    } finally { starting(false); }
  }
  window.headbopAuth = { get csrf() { return csrf; }, ensure, expired, refreshProjects };
  el("project-history").addEventListener("change", () => { el("open-project-button").disabled = !el("project-history").value; });
  el("project-history-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const id = el("project-history").value;
    if (id && csrf) document.dispatchEvent(new CustomEvent("headbop:open-project", { detail: { id } }));
  });
  function route() {
    if (window.location.hash === "#studio") openStudio();
    else {
      showHome();
      if (window.location.hash === "#how-it-works") el("how-it-works").scrollIntoView();
      else window.scrollTo({ top: 0 });
    }
  }
  window.addEventListener("hashchange", route);
  // Demo motion is decorative, optional and respects the OS accessibility preference.
  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  let paused = motionPreference.matches;
  function updateDemo() {
    el("hero-demo").classList.toggle("paused", paused);
    el("demo-toggle").setAttribute("aria-pressed", String(paused));
    el("demo-toggle-icon").textContent = paused ? "▷" : "Ⅱ";
    i18n.bindAttribute(el("demo-toggle"), "aria-label", paused ? "home.play" : "home.pause");
  }
  el("demo-toggle").addEventListener("click", () => { paused = !paused; updateDemo(); });
  motionPreference.addEventListener("change", (event) => { paused = event.matches; updateDemo(); });
  updateDemo();
  route();
})();
