"use strict";
(() => {
  const agentId = location.pathname.split("/").filter(Boolean)[1];
  const base = `/public/agents/${encodeURIComponent(agentId)}`;
  const $ = (id) => document.getElementById(id);
  const input = $("message"), send = $("send"), reset = $("reset"), messages = $("messages");
  const storageKey = `agente-chat:${agentId}`;
  let agent, busy = false, closed = false, conversationId = null, transcript = [];
  function status(text, error = false) { $("status").textContent = text; $("status").classList.toggle("error", error); }
  function setBusy(value) {
    busy = value; input.disabled = value || closed; send.disabled = value || closed; reset.disabled = value;
    send.textContent = value ? "Enviando…" : "Enviar ↗";
  }
  function save() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({conversationId, transcript, closed})); } catch { /* Chat also works without browser storage. */ }
  }
  function addMessage(role, content, attachments = [], remember = true) {
    const block = document.createElement("article"); block.className = `message ${role === "user" ? "user" : "assistant"}`;
    const label = document.createElement("p"); label.className = "message-label"; label.textContent = role === "user" ? "Tú" : agent.name;
    const bubble = document.createElement("div"); bubble.className = "bubble"; appendLinkedText(bubble, content);
    block.append(label, bubble);
    const links = document.createElement("div"); links.className = "attachments";
    for (const file of attachments) {
      // Accept only downloads served by this agent, including restored browser state.
      if (typeof file.url !== "string" || !file.url.startsWith(`${base}/files/`)) continue;
      const link = document.createElement("a"); link.href = file.url; link.textContent = `↓ ${file.name}`; link.setAttribute("download", ""); links.append(link);
    }
    if (links.childElementCount) block.append(links);
    messages.append(block); messages.scrollTop = messages.scrollHeight;
    if (remember) { transcript.push({role, content, attachments}); save(); }
    return block; // Permite sustituir una vista provisional sin guardarla como respuesta definitiva.
  }
  function fresh() {
    conversationId = null; transcript = []; closed = false; messages.replaceChildren();
    addMessage("assistant", agent.welcome_message || "Hola, ¿en qué puedo ayudarte?");
    status(""); setBusy(false); input.value = ""; input.focus(); save();
  }
  async function jsonRequest(path, options = {}) {
    // Envía al backend del proyecto con la sesión del visitante; el navegador no recibe la clave del modelo.
    const response = await fetch(path, {credentials: "same-origin", cache: "no-store", ...options});
    // Decodifica la respuesta HTTP; un cuerpo no JSON se convierte en objeto vacío para tratar el error.
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof result.detail === "string" ? result.detail : "No se pudo completar la solicitud. Inténtalo de nuevo.");
      error.status = response.status; throw error;
    }
    return result;
  }
  async function init() {
    try {
      agent = await jsonRequest(base);
      document.title = `${agent.name} · Conversar`;
      $("name").textContent = agent.name; $("description").textContent = agent.description;
      for (const item of (agent.response_links || [])) {
        const link = document.createElement("a"); link.href = item.url; link.textContent = item.label;
        link.target = "_blank"; link.rel = "noopener noreferrer"; $("agent-links").append(link);
      }
      const avatar = $("avatar"); let avatarUrl = agent.logo_url;
      if (!avatarUrl && /^https?:\/\//i.test(agent.avatar || "")) {
        try { const url = new URL(agent.avatar); if (!url.username && !url.password) avatarUrl = url.href; } catch { /* Use default avatar. */ }
      }
      if (avatarUrl) {
        const picture = document.createElement("img"); picture.src = avatarUrl; picture.alt = ""; picture.referrerPolicy = "no-referrer";
        picture.addEventListener("error", () => { avatar.textContent = "🤖"; }); avatar.replaceChildren(picture);
      } else { avatar.textContent = (agent.avatar || "🤖").slice(0, 8); }
      let restored;
      try { restored = JSON.parse(sessionStorage.getItem(storageKey)); } catch { /* Start fresh. */ }
      if (restored && Array.isArray(restored.transcript) && restored.transcript.length &&
          restored.transcript.every((entry) => typeof entry.content === "string" && ["user", "assistant"].includes(entry.role))) {
        conversationId = typeof restored.conversationId === "string" ? restored.conversationId : null;
        closed = !!restored.closed; transcript = restored.transcript;
        for (const entry of transcript) addMessage(entry.role, entry.content, Array.isArray(entry.attachments) ? entry.attachments : [], false);
        status(closed ? "La conversación finalizó. Puedes iniciar una nueva." : ""); setBusy(false);
      } else { fresh(); }
    } catch (error) {
      $("name").textContent = "Página no disponible"; status(error.message, true);
    }
  }
  reset.addEventListener("click", fresh);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("chat-form").requestSubmit(); }
  });
  $("chat-form").addEventListener("submit", async (event) => {
    // Intercepta el formulario y normaliza los espacios exteriores de la pregunta actual.
    event.preventDefault(); const message = input.value.trim();
    // Evita un segundo envío mientras hay una petición pendiente o la conversación está cerrada.
    if (!message || busy || closed || !agent) return;
    // Bloquea controles hasta recibir la respuesta: un doble clic no debe repetir una generación.
    setBusy(true); status("El agente está preparando su respuesta…");
    // La pregunta y el texto provisional se muestran enseguida; solo done/JSON se guarda en el historial local.
    let pendingQuestion = addMessage("user", message, [], false), partial = null;
    try {
      // Solo envía pregunta e ID. El servidor carga el historial y añade instrucciones, RAG y herramientas.
      const response = await fetch(`${base}/chat`, {method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json", "X-Chat-Request": "1", "Accept": "application/x-ndjson"},
        body: JSON.stringify({message, conversation_id: conversationId})});
      const result = await readChatResponse(response, (snapshot) => {
        if (!partial) partial = addMessage("assistant", "", [], false);
        // Los snapshots pueden contener Markdown incompleto: nunca se interpretan como HTML ni enlaces.
        partial.querySelector(".bubble").textContent = snapshot;
        messages.scrollTop = messages.scrollHeight;
        status("El agente está respondiendo…");
      });
      // Conserva el ID del servidor para que la siguiente pregunta use la misma memoria persistida.
      conversationId = result.conversation_id; input.value = "";
      // Sustituye las vistas provisionales: la pregunta queda una sola vez y los enlaces se renderizan al finalizar.
      pendingQuestion.remove(); pendingQuestion = null;
      if (partial) { partial.remove(); partial = null; }
      addMessage("user", message); addMessage("assistant", result.response, result.attachments || []);
      closed = result.conversation_status === "CLOSED";
      status(result.error || (closed ? "La conversación finalizó. Puedes iniciar una nueva." : ""), !!result.error); save();
    } catch (error) {
      status(error.message, true);
      if (error.status === 401) {
        await jsonRequest(base).catch(() => {}); closed = true;
        status("Tu sesión ha caducado. Pulsa Nueva conversación para continuar.", true);
      } else if (error.status === 403) {
        closed = true; status(`${error.message}. Pulsa Nueva conversación para continuar.`, true);
      } else if (error.status === 404) { closed = true; }
    } finally {
      // Un fallo de red o de validación no deja texto incompleto presentado como respuesta final.
      if (pendingQuestion) pendingQuestion.remove();
      if (partial) partial.remove();
      setBusy(false); if (!closed) input.focus();
    }
  });
  init();
})();
