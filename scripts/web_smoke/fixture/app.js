// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Atlas Omnia
// web_smoke fixture page logic, written from docs/web-protocol.md sections 6, 7 and 9 only.
// It lets the harness exercise the stub and the plugin before the real bundle exists.
"use strict";
(function () {
  // 7.2: at top level, before any other script: read and strip the fragment.
  const match = /^#launch=([A-Za-z0-9_-]{43})$/.exec(location.hash);
  let launchCode = match ? match[1] : null;
  history.replaceState(null, "", "/ui/index.html");
  const STRICT_PROFILE = true; // fixture.py flips this for the negative control bundle only
  let session = null;          // closure only (R7)
  let turn = null;             // {id, seq, have}
  let statePoll = null;
  let ended = false;
  const TEXTS = {
    null: "Click Open Sidecar in LibreOffice to reconnect.",
    replaced: "This document is open in a newer Sidecar window. This one is disconnected.",
    host_gone: "LibreOffice closed this document.",
    turned_off: "The LibreOffice plugin was turned off in Hermes.",
    idle: "This window was idle too long. Click Open Sidecar in LibreOffice again.",
    double_use: "This launch was used by another program; that connection was closed. Click Open Sidecar again.",
    evicted: "Too many Sidecar windows are open, so this one was disconnected. Click Open Sidecar in LibreOffice again.",
  };
  const $ = (id) => document.getElementById(id);

  function api(method, path, body, keepalive) {
    const headers = {};
    if (session) headers["X-Sidecar-Session"] = session;
    const init = { method, headers, credentials: "omit", cache: "no-store" };
    if (body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    if (keepalive) init.keepalive = true;
    return fetch(path, init).then((r) => r.json().then((data) => ({ status: r.status, data })));
  }

  // Write the live region only when its text changes, so a 3 s state poll is not re-announced (G5).
  function setStatus(text) { const el = $("connection-status"); if (el.textContent !== text) el.textContent = text; }
  function showConnect(text) {
    $("desk-connect").hidden = false;
    $("desk-connect-text").textContent = text;
    setStatus("Disconnected");
  }
  function endWith(reason) {
    if (ended) return;
    ended = true;
    session = null;
    turn = null;
    if (statePoll) clearInterval(statePoll);
    $("send-button").disabled = true;
    $("stop-button").disabled = true;
    $("new-chat-button").disabled = true;
    showConnect(Object.prototype.hasOwnProperty.call(TEXTS, reason) ? TEXTS[reason] : TEXTS[null]);
    if (reason === "host_gone") setTimeout(() => window.close(), 200);
  }
  function handleAuth(res) {
    if (res.status === 401 && res.data && res.data.code === "no_session") { endWith(res.data.ended); return true; }
    if (res.status === 503 && res.data && res.data.code === "turned_off") { endWith("turned_off"); return true; }
    return false;
  }

  function addMessage(role) {
    const article = document.createElement("article");
    article.className = "message " + role;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    article.appendChild(bubble);
    $("messages").appendChild(article);
    return bubble;
  }

  // 7.2 / R1 / R19: the only HTML sink is DOMPurify's Trusted Types output.
  function render(markdown, bubble) {
    const html = window.marked.parse(markdown, { async: false, breaks: true });
    const strict = {
      ALLOWED_TAGS: ["p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2",
        "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "hr", "a"],
      ALLOWED_ATTR: ["href", "align", "start"], ADD_URI_SAFE_ATTR: ["align", "start"], ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false,
      ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i, RETURN_TRUSTED_TYPE: true,
    };
    const clean = window.DOMPurify.sanitize(html, STRICT_PROFILE ? strict : { RETURN_TRUSTED_TYPE: true });
    const holder = document.createElement("div");
    holder.innerHTML = clean;
    if (STRICT_PROFILE) {
      for (const a of Array.from(holder.querySelectorAll("a"))) {
        const url = a.getAttribute("href");
        const span = document.createElement("span");
        span.className = "link-control";
        span.appendChild(document.createTextNode(a.textContent));
        if (url) {
          const shown = document.createElement("span");
          shown.className = "link-url";
          shown.textContent = " (" + url + ") ";
          const copy = document.createElement("button");
          copy.type = "button";
          copy.textContent = "Copy link";
          copy.addEventListener("click", () => {
            if (navigator.clipboard) navigator.clipboard.writeText(url).catch(() => {});
          });
          span.appendChild(shown);
          span.appendChild(copy);
        }
        a.replaceWith(span);
      }
    }
    while (holder.firstChild) bubble.appendChild(holder.firstChild);
  }

  function setWorking(working) {
    const blocked = working || !!pending || newChatPending;
    $("stop-button").disabled = !working;
    $("send-button").disabled = blocked;
    $("prompt-input").disabled = !!pending || newChatPending;
    $("new-chat-button").disabled = blocked;
  }

  function turnStatus(t) {
    if (t.state === "stopping") return "Stopping…";
    if (t.hermes_state === "waiting_for_approval")
      return "Hermes asked for an approval that Sidecar cannot give. Press Stop and ask in Hermes itself.";
    return "Hermes is working…";
  }

  function finishTurn(t) {
    const o = t.outcome || { kind: "error", message: "the chat endpoint failed" };
    if (o.kind === "completed") render(turn.text, addMessage("assistant"));
    else addMessage("assistant notice").textContent = o.message;
    turn = null;
    setWorking(false);
    setStatus("Ready");
    $("prompt-input").focus();
  }

  function followTurn() {
    if (!turn || ended) return;
    const t = turn;
    api("GET", "/api/turn?id=" + t.id + "&seq=" + t.seq + "&have=" + t.text.length).then((res) => {
      if (handleAuth(res) || turn !== t) return;
      if (res.status !== 200) { setTimeout(followTurn, 1000); return; }
      const view = res.data.turn;
      if (view.text_from === t.text.length) t.text += view.text;
      t.seq = view.seq;
      if (res.data.superseded) return;
      if (view.state === "done") { finishTurn(view); return; }
      setStatus(turnStatus(view));
      followTurn();
    }, () => setTimeout(followTurn, 1000));
  }

  // Contract revision 4 (6.3, 7.2): one send_id per Send, the same id on every retry; a turn the
  // state poll reports that this window has not followed is followed, also when ended, but while a
  // Send is undecided only its own turn (same send_id) decides it; nothing is concluded from a
  // state without it. Try again re-sends the same send_id. State answers from before a Send or a
  // New chat are dropped (epoch); New chat never overlaps a Send.
  const seen = new Set();
  let pending = null;
  let newChatPending = false;
  let epoch = 0;
  function settle() {
    if (pending && pending.unknown) $("desk-connect").hidden = true;
    pending = null;
  }
  function startTurn(view) {
    seen.add(view.id);
    settle();
    turn = { id: view.id, seq: view.seq || 0, text: "" };
    setWorking(true);
    setStatus(turnStatus(view));
    followTurn();
  }
  function newSendId() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function deliver(p) {
    if (pending !== p || !session) return;
    p.tries += 1;
    api("POST", "/api/chat", { message: p.message, send_id: p.id }).then((res) => {
      if (handleAuth(res) || pending !== p) return;
      if ((res.status === 202 || res.status === 200) && res.data.turn) { startTurn(res.data.turn); return; }
      if (res.data.code === "endpoint_busy" || (res.data.code === "busy" && p.tries > 1)) { undecided(p); return; }
      settle();
      addMessage("assistant notice").textContent = res.data.error;
      setWorking(false);
    }, () => { if (pending === p) undecided(p); });
  }
  function undecided(p) {
    if (p.tries < 3) { setTimeout(() => deliver(p), 1000); return; }
    if (!p.unknown) addMessage("assistant notice").textContent = "It is not known yet whether Hermes received this message.";
    p.unknown = true;   // its own turn in a state poll, or Try again with the same send_id, decides
    $("desk-connect").hidden = false;
    $("desk-connect-text").textContent = "Hermes has not confirmed your last message. Try again asks once more.";
    $("desk-connect-button").hidden = false;
  }
  function retryPending() {
    if (!pending || !pending.unknown) return;
    pending.tries = 2;
    deliver(pending);
  }
  function send() {
    const input = $("prompt-input");
    const message = input.value;
    if (!session || turn || pending || newChatPending || !message.trim()) return;
    addMessage("user").textContent = message;
    input.value = "";
    input.focus();
    epoch += 1;
    pending = { id: newSendId(), message, tries: 0, unknown: false };
    setWorking(false);
    deliver(pending);
  }

  function stop() {
    if (!turn) return;
    api("POST", "/api/turn/stop", { turn: turn.id }).then((res) => {
      if (handleAuth(res)) return;
      if (res.data && res.data.turn && res.data.turn.state === "stopping") setStatus("Stopping…");
      $("prompt-input").focus();
    });
  }

  function applyState(data) {
    $("bridge-session-id").textContent = data.chat_label || "";
    $("bridge-status").textContent = data.chat.ready ? "LibreOffice connected" : "Chat unavailable: " + data.chat.reason;
    if (!turn) setStatus(data.chat.ready ? "Ready" : "Chat is not ready (" + data.chat.reason + ")");
    if (data.turn && !turn && !seen.has(data.turn.id) && (!pending || (pending.unknown && data.turn.send_id === pending.id))) {
      startTurn(Object.assign({}, data.turn, { seq: 0 }));
    }
  }

  function pollState() {
    if (!session || turn || newChatPending || document.visibilityState !== "visible") return;
    const asked = epoch;
    api("GET", "/api/state").then((res) => {
      if (asked === epoch && !handleAuth(res) && res.status === 200) applyState(res.data);
    });
  }

  function start() {
    const code = launchCode;
    launchCode = null;
    if (!code) { endWith(null); return; }
    api("POST", "/api/session", { launch: code }).then((res) => {
      if (res.status !== 200) {
        ended = true;
        showConnect(res.data && res.data.error ? res.data.error : TEXTS[null]);
        return;
      }
      session = res.data.session;
      $("document-line").textContent = res.data.document.title;
      $("bridge-url").textContent = res.data.endpoint;
      $("new-chat-button").disabled = false;
      applyState(res.data);
      statePoll = setInterval(pollState, 3000);
      window.addEventListener("focus", pollState);
      document.addEventListener("visibilitychange", pollState);
      $("prompt-input").focus();
    }, () => showConnect(TEXTS[null]));
  }

  document.addEventListener("DOMContentLoaded", () => {
    const input = $("prompt-input");
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && turn && !$("settings-dialog").open) { e.preventDefault(); stop(); }
    });
    $("send-button").addEventListener("click", send);
    $("stop-button").addEventListener("click", stop);
    $("desk-connect-button").addEventListener("click", retryPending);
    $("new-chat-button").addEventListener("click", () => {
      if (!session || turn || pending || newChatPending) return;
      newChatPending = true;
      epoch += 1;
      setWorking(false);
      api("POST", "/api/new-chat", {}).then((res) => {
        newChatPending = false;
        epoch += 1;
        setWorking(!!turn);
        if (handleAuth(res)) return;
        if (res.status === 200) seen.clear();
        if (res.status === 200) { $("messages").replaceChildren(); $("bridge-session-id").textContent = res.data.chat_label; }
        else addMessage("assistant notice").textContent = res.data.error;
      });
    });
    const dialog = $("settings-dialog");
    $("settings-button").addEventListener("click", () => dialog.showModal());
    dialog.addEventListener("close", () => $("settings-button").focus());
    dialog.addEventListener("keydown", (e) => {   // keep Tab inside the open dialog (G5)
      if (e.key !== "Tab") return;
      const items = Array.from(dialog.querySelectorAll("button, input:checked, input[type=radio]:not([name]), [tabindex]"))
        .filter((el) => !el.disabled);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    });
    window.addEventListener("pagehide", () => {
      if (session) api("POST", "/api/session/close", {}, true).catch(() => {});
    });
    start();
  });
})();
