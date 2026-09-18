"""Compara localmente el payload anterior y el optimizado con datos sintéticos.

Ejecutar desde la raíz: python scripts/audit_prompt_size.py
No lee claves, no llama a proveedores y no estima tokens ni precios.
"""

import json  # Reproduce la serialización anterior para la comparación.
import sys  # Permite importar backend sin instalar el proyecto como paquete.
from pathlib import Path  # Resuelve la raíz a partir de este archivo.

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # Importación local del runtime.
from app.runtime.prompt_budget import bounded_history, build_evidence, compact_json  # noqa: E402


def main():
    question = "¿Cuál es el horario de atención?"  # Pregunta de ejemplo, sin datos de usuarios.
    content = "El horario de atención es de lunes a viernes de nueve a cinco. " * 12  # Fragmento sintético.
    record = {"content": content, "page_content": content,  # Los proveedores mantienen ambos aliases.
              "metadata": {"source_id": "manual", "source": "Manual.txt", "answer": content}}
    messages = [{"role": "user", "content": "Necesito información de soporte"},
                {"role": "assistant", "content": "Puedo ayudarte"},
                {"role": "user", "content": question}]  # Estado después de ConversationGuard.
    evidence = {"faqs": [record], "documents": [record, record], "tools": []}  # Incluye un duplicado.
    old_context = json.dumps(evidence, ensure_ascii=False, default=str)[:32000]  # Recorte anterior.
    before = json.dumps({"history": messages[-20:], "question": question, "evidence": old_context},
                        ensure_ascii=False, default=str)  # JSON incluido como cadena y pregunta repetida.
    context = json.loads(build_evidence([record], [record, record], [], 32000))  # Evidencia optimizada.
    after = compact_json({"history": bounded_history(messages, question, 20, 16000),
                          "question": question, "evidence": context})  # Una sola serialización final.
    report = {"scenario": "synthetic_payload_only", "before_chars": len(before), "after_chars": len(after),
              "saved_chars": len(before) - len(after),
              "reduction_percent": round(100 * (1 - len(after) / len(before)), 2),
              "external_calls": 0, "measures_tokens": False}  # Mide caracteres del payload, no el prompt system.
    print(json.dumps(report, ensure_ascii=False, indent=2))  # Resultado reproducible sin contenido privado.


if __name__ == "__main__":  # Importar este módulo no ejecuta la comparación automáticamente.
    main()  # Ejecuta exclusivamente el ejemplo local.
