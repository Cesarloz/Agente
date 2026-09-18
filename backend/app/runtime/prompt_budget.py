"""Presupuestos locales de texto: no llaman al modelo ni estiman tokens facturados.

El tamaño se mide sobre JSON serializado, incluidos escapes. Se conservan objetos
válidos y se señala cualquier recorte; nunca se corta una cadena JSON a la mitad.
"""

import json  # Serializa evidencia e historial sin depender del SDK del proveedor.
import math  # Detecta NaN e infinitos que una consulta SQL puede devolver como float.
from copy import deepcopy  # Evita modificar las fuentes que se guardan en auditoría.
from itertools import zip_longest  # Alterna fuentes para que las FAQ no agoten todo el presupuesto.


def compact_json(value):
    """Elimina espacios de formato, conserva Unicode y convierte fechas a texto."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
    except ValueError:
        # Solo recorre de nuevo resultados excepcionales: JSON estándar no admite NaN/Infinity numéricos.
        def safe_numbers(item):
            if isinstance(item, float) and not math.isfinite(item):
                return str(item)  # Conserva el valor especial como texto, sin inventar un cero o valor finito.
            if isinstance(item, dict):
                return {key: safe_numbers(child) for key, child in item.items()}  # Copia sin mutar resultados SQL.
            if isinstance(item, (list, tuple)):
                return [safe_numbers(child) for child in item]  # Revisa filas y estructuras anidadas.
            return item  # Tipos habituales conservan su serialización original.
        return json.dumps(safe_numbers(value), ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)


def compact_sources(records):
    """Envía una copia del texto por fragmento y solo metadatos útiles al modelo."""
    result, seen = [], set()  # La deduplicación vive únicamente durante esta construcción.
    for record in records:
        content = str(record.get("content") or record.get("page_content") or "")  # Unifica aliases.
        metadata = dict(record.get("metadata") or {})  # Copia: no altera la auditoría original.
        if not metadata.get("source_id"):
            metadata["source_id"] = metadata.get("faq_id") or metadata.get("document_id")  # Compatibilidad de hooks.
        identity = (metadata.get("source_id"), metadata.get("source"), content)  # Conserva fuentes distintas.
        if not content or identity in seen:
            continue  # No paga de nuevo por fragmentos idénticos de la misma fuente.
        seen.add(identity)  # Registra el fragmento antes de procesar el siguiente.
        result.append({"content": content, "metadata": {  # Omite answer duplicado, tags y campos internos.
            key: metadata[key] for key in ("source_id", "source", "page", "chunk_index", "file_id")
            if metadata.get(key) is not None
        }})
    return result  # El original sigue disponible para trazas y referencias persistidas.


def _fit_item(item, maximum):
    """Recorta texto o filas completas hasta que un elemento cabe por sí solo."""
    if len(compact_json(item)) <= maximum:
        return item  # Mantiene intacto cualquier resultado que ya cabe.
    candidate = deepcopy(item)  # Los recortes no alteran la salida real de la herramienta.
    candidate["truncated"] = True  # El modelo debe saber que la evidencia es parcial.
    if isinstance(candidate.get("result"), dict) and isinstance(candidate["result"].get("rows"), list):
        result = candidate["result"]  # Los resultados SQL conservan su estructura y estado ok/error.
        rows = result["rows"]  # Referencia a las filas de la copia, nunca al resultado original.
        result["original_row_count"] = len(rows)  # Distingue filas recibidas de filas enviadas al modelo.
        low, high, best = 0, len(rows), None  # Búsqueda logarítmica, incluso con cientos de filas SQL.
        while low <= high:
            middle = (low + high) // 2  # Prueba un prefijo de filas completas, nunca celdas cortadas.
            result["rows"] = rows[:middle]  # No modifica la lista original de filas de la copia.
            result["row_count"] = middle  # Mantiene el contador coherente con las filas visibles.
            if len(compact_json(candidate)) <= maximum:
                best = deepcopy(candidate)  # Conserva el prefijo más largo que ha cabido.
                low = middle + 1  # Intenta añadir filas si queda espacio.
            else:
                high = middle - 1  # Reduce filas cuando se excede el presupuesto.
        return best  # None indica que ni los metadatos SQL caben; se omite el resultado completo.
    field = "content" if "content" in candidate else "result"  # Documentos o herramientas de texto.
    text = candidate.get(field)
    if not isinstance(text, str):
        return None  # No inventa un resumen de objetos que no sabe recortar de forma segura.
    low, high, best = 0, len(text), None  # Busca el mayor prefijo que cabe, contando escapes JSON.
    while low <= high:
        middle = (low + high) // 2  # La búsqueda binaria evita serializar cada longitud posible.
        candidate[field] = text[:middle]  # La bandera separada informa del recorte sin alterar el texto.
        if len(compact_json(candidate)) <= maximum:
            best = deepcopy(candidate)  # Guarda una copia porque la siguiente iteración modifica candidate.
            low = middle + 1  # Intenta aprovechar más del presupuesto restante.
        else:
            high = middle - 1  # Reduce texto cuando la serialización todavía supera el máximo.
    return best  # None significa que ni los metadatos caben; se omite el elemento completo.


def build_evidence(faqs, documents, tools, maximum=32000):
    """Construye JSON válido acotado y evita que una clase de fuente monopolice el contexto."""
    evidence = {"faqs": [], "documents": [], "tools": [], "truncated": False}  # Esqueleto estable.
    size = len(compact_json(evidence))  # Incluye llaves, nombres de campos y arrays vacíos.
    if maximum < size:
        raise ValueError("El presupuesto de contexto no permite representar la evidencia")
    groups = (list(tools), compact_sources(faqs), compact_sources(documents))  # Prioriza herramientas.
    complete = compact_json({"faqs": groups[1], "documents": groups[2], "tools": groups[0], "truncated": False})
    if len(complete) <= maximum:
        return complete  # No sacrifica información por cuotas de reparto cuando todo ya cabe.
    count = sum(len(group) for group in groups)  # Una única fuente puede aprovechar todo el presupuesto.
    for items in zip_longest(*groups):
        for key, item in zip(("tools", "faqs", "documents"), items):
            if item is None:
                continue  # Una lista agotada no impide seguir con las otras fuentes.
            separator = int(bool(evidence[key]))  # Cada elemento adicional necesita una coma.
            # Reserva como máximo la mitad del espacio inicial por elemento para diversificar fuentes.
            allowance = min(maximum - size - separator, max(256, (maximum - 80) // 2)) if count > 1 else maximum - size - separator
            fitted = _fit_item(item, allowance)  # Conserva estructura y señala recortes.
            if fitted is None:
                evidence["truncated"] = True  # Registra también elementos omitidos por completo.
                continue
            evidence[key].append(fitted)  # Añade únicamente objetos que caben en el presupuesto.
            size += len(compact_json(fitted)) + separator  # Acumula coste sin reserializar todo el contexto.
            if fitted.get("truncated"):
                evidence["truncated"] = True  # Advertencia global además de la marca del fragmento.
    return compact_json(evidence)  # true ocupa un carácter menos que false: el cálculo es conservador.


def bounded_history(messages, question, window, maximum):
    """Conserva mensajes recientes completos; la pregunta actual se envía por separado."""
    previous = list(messages)  # Copia superficial suficiente: no modificamos mensajes individuales.
    if previous and previous[-1].get("role") == "user" and previous[-1].get("content") == question:
        previous.pop()  # ConversationGuard ya añadió esta pregunta; evita pagar dos veces por ella.
    if window <= 0 or maximum < 2:
        return []  # Memoria desactivada o sin presupuesto; no afecta la persistencia en PostgreSQL.
    history, size = [], 2  # Cuenta los corchetes del array serializado.
    for message in reversed(previous[-window:]):
        clean = {"role": message["role"], "content": message["content"]}  # Excluye IDs, fechas y adjuntos.
        cost = len(compact_json(clean)) + int(bool(history))  # Incluye la coma si ya existe un mensaje.
        if size + cost > maximum:
            break  # Retiene un sufijo continuo: no une mensajes antiguos saltándose contexto intermedio.
        history.insert(0, clean)  # Restablece el orden cronológico esperado por el modelo.
        size += cost  # Los mensajes cuentan por longitud serializada, no solo por cantidad.
    return history  # Nunca corta instrucciones o datos del usuario dentro de un mensaje histórico.
