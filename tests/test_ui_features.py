from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from admin.api import api


def button(app, label):
    return next(item for item in app.button if item.label == label)


def input_by_label(app, label, value):
    next(item for item in app.text_input if item.label == label).input(value)


def test_gmail_form_saves_credentials_without_sending_a_message():
    app = AppTest.from_string("from admin.settings import gmail_settings\ngmail_settings()", default_timeout=20)
    with patch.object(api, "get", return_value={"configured": False}), patch.object(api, "put", return_value={"configured": True}) as configure, patch.object(api, "post") as send:
        app.run()
        input_by_label(app, "Dirección Gmail", "owner@gmail.com")
        input_by_label(app, "Contraseña de aplicación", "abcdefghijklmnop")
        button(app, "Guardar Gmail").click().run()
    assert not app.exception
    configure.assert_called_once_with("/api/email/gmail", {"sender": "owner@gmail.com", "sender_name": "ReflexIA", "app_password": "abcdefghijklmnop"})
    send.assert_not_called()


def test_email_composer_keeps_request_id_for_duplicate_submissions():
    source = """
from admin.overview import email_composer
email_composer({"id":"conversation", "agent_id":"agent", "messages":[{"role":"assistant", "content":"Respuesta con https://outlook.office.com/bookwithme/"}]})
"""
    def read(path, **kwargs):
        return {"configured": True, "sender": "owner@gmail.com"} if path == "/api/email/gmail" else [{"id": "job", "status": "PENDING"}]
    with patch.object(api, "get", side_effect=read), patch.object(api, "post", return_value={"job_id": "job", "status": "PENDING"}) as enqueue:
        app = AppTest.from_string(source, default_timeout=20).run()
        input_by_label(app, "Correo del destinatario", "person@example.com")
        next(item for item in app.text_area if item.label == "Información adicional").input("Información adicional revisada")
        button(app, "Enviar correo por Gmail").click().run()
        first = enqueue.call_args.kwargs["json"]
        button(app, "Enviar correo por Gmail").click().run()
        second = enqueue.call_args.kwargs["json"]
    assert not app.exception
    assert first["request_id"] == second["request_id"]
    assert first["recipient"] == "person@example.com"
    assert "https://outlook.office.com/bookwithme/" in first["body"]
    assert first["body"].endswith("Información adicional revisada")


def test_links_form_saves_bookings_url():
    source = "from admin.agents import response_links\nresponse_links({'id':'agent'})"
    with patch.object(api, "patch") as update:
        app = AppTest.from_string(source, default_timeout=20).run()
        app.text_area[0].input("Agendar una cita | https://outlook.office.com/bookwithme/")
        button(app, "Guardar enlaces").click().run()
    assert not app.exception
    update.assert_called_once_with("/api/agents/agent", {"response_links": [{"label": "Agendar una cita", "url": "https://outlook.office.com/bookwithme/"}]})
