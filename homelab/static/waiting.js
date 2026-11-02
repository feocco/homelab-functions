(() => {
  "use strict";

  const POLL_MS = 30000;
  const FALLBACK_AFTER_MS = 8000;
  const STALE_MS = 45000;
  const SSE_RETRY_MS = 60000;
  const NEW_MS = 8000;
  const LEAVE_MS = 320;
  const MODE_LABELS = {
    connecting: "Connecting",
    live: "Live",
    reconnecting: "Reconnecting",
    polling: "Polling every 30s",
    offline: "Offline",
  };
  const UNITS = [["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];

  const $ = (selector, root = document) => root.querySelector(selector);
  const liveEl = $("#live");
  const updatedEl = $("#updated");
  const sectionsRoot = $("#sections");
  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const cards = new Map();
  const sections = new Map();

  let snapshot = null;
  let seen = null;
  let mode = "connecting";
  let source = null;
  let lastMessage = Date.now();
  let pollTimer = null;
  let fallbackTimer = null;
  let retryTimer = null;

  function ago(iso) {
    const time = Date.parse(iso);
    if (Number.isNaN(time)) return "";
    const seconds = Math.round((time - Date.now()) / 1000);
    if (Math.abs(seconds) < 10) return "just now";
    for (const [unit, size] of UNITS) {
      if (Math.abs(seconds) >= size || unit === "second") {
        return relative.format(Math.round(seconds / size), unit);
      }
    }
    return "";
  }

  function safeHref(url) {
    return typeof url === "string" && url.startsWith("https://") ? url : null;
  }

  function withCode(element, text) {
    const parts = String(text).split("`");
    element.replaceChildren(
      ...parts.map((part, index) => {
        if (index % 2 === 0) return document.createTextNode(part);
        const code = document.createElement("code");
        code.textContent = part;
        return code;
      }),
    );
  }

  function newRequestId() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.className = "offscreen";
    document.body.append(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    if (!copied) throw new Error("copy failed");
  }

  function setMode(next) {
    mode = next;
    liveEl.dataset.mode = next;
    $(".live-label", liveEl).textContent = MODE_LABELS[next];
  }

  function render(next) {
    snapshot = next;
    $("#demo").hidden = !next.demo;
    $("#policy").textContent = next.feedback.policy;
    renderSummary(next);
    renderNotices(next);

    const firstRender = seen === null;
    const nextSeen = new Set(next.items.map((item) => item.id));
    for (const category of next.categories) {
      const items = next.items.filter((item) => item.category === category.id);
      const sectionEl = sectionFor(category);
      sectionEl.hidden = items.length === 0;
      $(".count", sectionEl).textContent = String(items.length);
      const list = $(".items", sectionEl);
      items.forEach((item, index) => {
        let card = cards.get(item.id);
        if (!card) {
          card = createCard(item);
          cards.set(item.id, card);
          if (!firstRender && !seen.has(item.id)) markNew(card);
        }
        updateCard(card, item, next.feedback);
        if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
      });
    }
    for (const [id, card] of cards) {
      if (!nextSeen.has(id)) {
        cards.delete(id);
        leave(card);
      }
    }
    seen = nextSeen;
    $("#empty").hidden = next.items.length > 0 || next.github.status !== "ok";
    renderUpdated();
  }

  function leave(card) {
    if (reduceMotion.matches) {
      card.remove();
      return;
    }
    card.classList.add("is-leaving");
    setTimeout(() => card.remove(), LEAVE_MS);
  }

  function renderSummary(next) {
    const total = next.items.length;
    let text;
    if (total === 0) {
      text = next.github.status === "ok" ? "Nothing needs you right now." : "No items to show.";
    } else {
      text = `${total} thing${total === 1 ? " needs" : "s need"} you.`;
    }
    $("#summary").textContent = text;
  }

  function renderNotices(next) {
    const notices = [];
    const github = next.github;
    if (github.status === "unconfigured") notices.push(["error", github.message]);
    if (github.status === "error") {
      const stale = github.refreshed_at ? ` Showing what was read ${ago(github.refreshed_at)}.` : "";
      notices.push(["error", `GitHub: ${github.message}${stale}`]);
    }
    if (!next.feedback.enabled) notices.push(["info", next.feedback.message]);
    $("#notices").replaceChildren(
      ...notices.map(([kind, text]) => {
        const notice = document.createElement("p");
        notice.className = `notice ${kind}`;
        notice.setAttribute("role", kind === "error" ? "alert" : "note");
        notice.textContent = text;
        return notice;
      }),
    );
  }

  function renderUpdated() {
    const github = snapshot && snapshot.github;
    if (!github) return;
    if (github.refreshed_at) {
      updatedEl.textContent = `Updated ${ago(github.refreshed_at)}`;
      updatedEl.dateTime = github.refreshed_at;
      updatedEl.title = new Date(github.refreshed_at).toLocaleString();
    } else {
      updatedEl.textContent = github.status === "pending" ? "Reading GitHub…" : "Not updated yet";
    }
  }

  function sectionFor(category) {
    let sectionEl = sections.get(category.id);
    if (!sectionEl) {
      sectionEl = $("#section-template").content.firstElementChild.cloneNode(true);
      sectionEl.dataset.category = category.id;
      $(".section-label", sectionEl).textContent = category.label;
      sectionsRoot.append(sectionEl);
      sections.set(category.id, sectionEl);
    }
    return sectionEl;
  }

  function markNew(card) {
    card.classList.add("is-new");
    setTimeout(() => card.classList.remove("is-new"), NEW_MS);
  }

  function createCard(item) {
    const card = $("#item-template").content.firstElementChild.cloneNode(true);
    card.dataset.id = item.id;
    const form = $(".feedback-form", card);
    $(".action-copy", card).addEventListener("click", () => copyAction(card));
    $(".done", card).addEventListener("click", () => markDone(card));
    $(".feedback-toggle", card).addEventListener("click", () => {
      if (form.hidden) openForm(card);
      else closeForm(card);
    });
    $(".cancel", card).addEventListener("click", () => closeForm(card));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      sendFeedback(card);
    });
    return card;
  }

  function updateCard(card, item, feedback) {
    card.dataset.category = item.category;
    card.item = item;
    withCode($(".need", card), item.need);
    withCode($(".why", card), item.why);

    const source = $(".source", card);
    source.textContent = item.source.label;
    source.href = safeHref(item.source.url) || "#";

    const open = $(".action-open", card);
    const copy = $(".action-copy", card);
    const copyBox = $(".copy-box", card);
    const isCopy = item.action.type === "copy";
    open.hidden = isCopy;
    copy.hidden = !isCopy;
    copyBox.hidden = !isCopy;
    if (isCopy) {
      $(".copy-value", card).textContent = item.action.value;
      if (!copy.dataset.copied) copy.textContent = item.action.label;
      copy.setAttribute("aria-label", `${item.action.label}: ${item.action.value}`);
    } else {
      open.textContent = item.action.label;
      open.href = safeHref(item.action.url) || "#";
    }

    const mergeOnly = item.category === "approve_merge";
    $(".done", card).hidden = mergeOnly;
    $(".clears", card).hidden = !mergeOnly;
    const toggle = $(".feedback-toggle", card);
    toggle.disabled = !feedback.enabled;
    toggle.title = feedback.enabled ? "" : feedback.message;
    if (!feedback.enabled && !$(".feedback-form", card).hidden) closeForm(card);
  }

  async function copyAction(card) {
    const button = $(".action-copy", card);
    try {
      await copyText(card.item.action.value);
      button.dataset.copied = "true";
      button.textContent = "Copied";
      setTimeout(() => {
        delete button.dataset.copied;
        button.textContent = card.item.action.label;
      }, 2000);
    } catch (error) {
      button.textContent = "Copy failed. Select the text above.";
    }
  }

  async function markDone(card) {
    const button = $(".done", card);
    button.disabled = true;
    try {
      const response = await fetch("/waiting/handled", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: card.dataset.id }),
      });
      if (response.status === 401) {
        location.reload();
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (cards.get(card.dataset.id) === card) {
        cards.delete(card.dataset.id);
        leave(card);
      }
    } catch (error) {
      button.disabled = false;
      button.textContent = "Done (try again)";
    }
  }

  function openForm(card) {
    $(".feedback-form", card).hidden = false;
    $(".feedback-toggle", card).setAttribute("aria-expanded", "true");
    $("textarea", card).focus();
  }

  function closeForm(card) {
    $(".feedback-form", card).hidden = true;
    $(".feedback-toggle", card).setAttribute("aria-expanded", "false");
  }

  function setFormStatus(card, text, kind) {
    const status = $(".form-status", card);
    status.textContent = text;
    status.className = `form-status ${kind}`;
  }

  async function sendFeedback(card) {
    const form = $(".feedback-form", card);
    const textarea = $("textarea", form);
    const text = textarea.value.trim();
    if (!text) {
      setFormStatus(card, "Write some feedback first.", "error");
      return;
    }
    if (form.dataset.requestText !== text) {
      form.dataset.requestId = newRequestId();
      form.dataset.requestText = text;
    }
    const send = $(".send", form);
    send.disabled = true;
    setFormStatus(card, "Sending…", "");
    try {
      const response = await fetch("/waiting/feedback", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          item_id: card.dataset.id,
          feedback: text,
          request_id: form.dataset.requestId,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.status === 401) {
        setFormStatus(card, "Your session ended. Reload the page to sign in.", "error");
        return;
      }
      if (!response.ok) {
        const message = body.error && body.error.message;
        setFormStatus(card, message || `Request failed with HTTP ${response.status}.`, "error");
        return;
      }
      textarea.value = "";
      delete form.dataset.requestText;
      setFormStatus(
        card,
        body.delivery === "launch"
          ? "Sent. A Cursor agent started and will open one pull request for you to review."
          : "Sent to this item's Cursor agent.",
        "ok",
      );
    } catch (error) {
      setFormStatus(card, "Could not reach homelab-functions. Sending again is safe.", "error");
    } finally {
      send.disabled = false;
    }
  }

  function heard() {
    lastMessage = Date.now();
    clearTimeout(fallbackTimer);
    fallbackTimer = null;
    stopPolling();
    setMode(navigator.onLine === false ? "offline" : "live");
  }

  function connect() {
    if (!("EventSource" in window)) {
      startPolling();
      return;
    }
    source = new EventSource("/waiting/events");
    source.addEventListener("snapshot", (event) => {
      heard();
      render(JSON.parse(event.data));
    });
    source.addEventListener("ping", heard);
    source.addEventListener("error", () => {
      if (mode === "live" || mode === "connecting") setMode("reconnecting");
      if (source.readyState === EventSource.CLOSED) {
        startPolling();
        clearTimeout(retryTimer);
        retryTimer = setTimeout(reconnect, SSE_RETRY_MS);
      } else {
        armFallback();
      }
    });
  }

  function reconnect() {
    if (source) source.close();
    connect();
  }

  function armFallback() {
    if (fallbackTimer) return;
    fallbackTimer = setTimeout(() => {
      fallbackTimer = null;
      if (mode !== "live") startPolling();
    }, FALLBACK_AFTER_MS);
  }

  function startPolling() {
    if (pollTimer) return;
    setMode("polling");
    poll();
    pollTimer = setInterval(poll, POLL_MS);
  }

  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  async function poll() {
    try {
      const response = await fetch("/waiting/snapshot", { credentials: "same-origin", cache: "no-store" });
      if (response.status === 401) {
        location.reload();
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
      if (pollTimer) setMode("polling");
    } catch (error) {
      setMode("offline");
    }
  }

  function tick() {
    renderUpdated();
    if (mode === "live" && Date.now() - lastMessage > STALE_MS) {
      setMode("reconnecting");
      armFallback();
      reconnect();
    }
  }

  window.addEventListener("offline", () => setMode("offline"));
  window.addEventListener("online", reconnect);

  render(JSON.parse($("#snapshot").textContent));
  connect();
  armFallback();
  setInterval(tick, 5000);
})();
