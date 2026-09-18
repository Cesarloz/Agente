"use strict";
// Consume una única respuesta HTTP: negociar streaming nunca provoca otro POST o un reintento.
async function readChatResponse(response, onText = () => {}) {
  const genericMessage = "No se pudo completar la solicitud. Inténtalo de nuevo.";
  const interruptedMessage = "La conexión se interrumpió antes de confirmar la respuesta.";
  function failure(message, status = 500) {
    const error = new Error(message); error.status = status; error.chatResponseError = true; return error;
  }
  function resultPayload(result) {
    if (!result || Array.isArray(result) || typeof result !== "object" || typeof result.response !== "string")
      throw failure("El servidor no devolvió una respuesta válida.");
    return result;
  }
  const contentType = (response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/x-ndjson") {
    // Los servidores anteriores pueden ignorar Accept y contestar JSON; se usa el mismo cuerpo recibido.
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw failure(typeof result.detail === "string" ? result.detail : genericMessage, response.status);
    return resultPayload(result);
  }
  if (!response.body || typeof response.body.getReader !== "function") throw failure(interruptedMessage);
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", {fatal: true}); // Conserva caracteres partidos entre paquetes, incluidos emoji.
  let pending = "";
  function eventLine(line) {
    if (!line.trim()) return null; // Tolera líneas vacías y finales CRLF.
    let event;
    try { event = JSON.parse(line); } catch { throw failure("El servidor envió una respuesta incompleta o inválida."); }
    if (!event || typeof event !== "object") throw failure("El servidor envió un evento inválido.");
    if (event.type === "ping") return null; // Mantiene la conexión sin modificar mensajes ni historial.
    if (event.type === "text" && typeof event.text === "string") {
      onText(event.text); return null; // El texto es un snapshot completo: reemplazar, nunca concatenar.
    }
    if (event.type === "done") return resultPayload(event.result); // Única confirmación definitiva del turno.
    if (event.type === "error") {
      const status = Number.isInteger(event.status) && event.status >= 400 && event.status <= 599 ? event.status : 500;
      throw failure(typeof event.message === "string" ? event.message : genericMessage, status);
    }
    throw failure("El servidor envió un evento inválido.");
  }
  try {
    while (true) {
      const {value, done} = await reader.read();
      pending += done ? decoder.decode() : decoder.decode(value, {stream: true});
      let newline;
      while ((newline = pending.indexOf("\n")) !== -1) {
        const line = pending.slice(0, newline); pending = pending.slice(newline + 1);
        const result = eventLine(line);
        if (result) return result; // Ignora cualquier dato posterior a la confirmación final.
      }
      if (done) {
        const result = eventLine(pending); // Acepta un último evento completo aunque falte el salto de línea.
        if (result) return result;
        throw failure(interruptedMessage); // Un EOF con solo texto parcial jamás equivale a una respuesta guardada.
      }
    }
  } catch (error) {
    if (error.chatResponseError) throw error;
    throw failure(interruptedMessage); // No expone excepciones de decodificación o transporte al usuario.
  } finally {
    try { await reader.cancel(); } catch { /* El servidor puede haber cerrado ya el transporte. */ }
    reader.releaseLock(); // Libera el lector al terminar, fallar o recibir done/error.
  }
}
