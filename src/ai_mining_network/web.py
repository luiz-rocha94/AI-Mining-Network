"""Loopback-only customer API for the local development UI."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .orchestrator import DemoError, DemoService


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AMNLocal/0.1"

    @property
    def service(self) -> DemoService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        print("[amn-web] " + fmt % args)

    def _json(self, status: int, payload: dict | list) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > 1_048_576:
            raise ValueError("request body exceeds 1 MiB")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON request body must be an object")
        return data

    def _multipart_body(self) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 1 or length > 27 * 1024 * 1024:
            raise ValueError("multipart request must be smaller than 27 MiB")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise ValueError("expected multipart/form-data")
        raw = self.rfile.read(length)
        headers = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
        message = BytesParser(policy=policy.default).parsebytes(headers + raw)
        if not message.is_multipart():
            raise ValueError("invalid multipart request")
        fields: dict[str, str] = {}
        files: list[tuple[str, bytes]] = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if not isinstance(name, str):
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is not None:
                if name != "files":
                    raise ValueError("uploaded files must use the 'files' field")
                files.append((filename, payload))
            else:
                try:
                    fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"form field {name!r} is not UTF-8 text") from exc
        return fields, files

    def _local_origin_allowed(self) -> bool:
        allowed = {"127.0.0.1", "localhost", "::1"}
        host = urlsplit("//" + self.headers.get("Host", "")).hostname
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.hostname in allowed

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_origin_allowed():
            self._json(403, {"error": "local API accepts loopback hosts and origins only"})
            return
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        try:
            if parts == ["api", "health"]:
                self._json(200, self.service.health())
                return
            if parts == ["api", "models"]:
                self._json(200, self.service.list_models())
                return
            if parts == ["api", "skills"]:
                self._json(200, self.service.list_skills())
                return
            if parts == ["api", "workflows"]:
                self._json(200, self.service.list_workflows())
                return
            if len(parts) == 3 and parts[:2] == ["api", "workflows"]:
                self._json(200, self.service.get_workflow(parts[2]))
                return
            if len(parts) == 3 and parts[:2] == ["api", "skills"]:
                self._json(200, self.service.skill_details(parts[2]))
                return
            if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
                self._json(200, self.service.status(parts[2]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "events":
                self._json(200, self.service.audit_events(parts[2]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "artifacts":
                self._json(200, self.service.artifacts(parts[2]))
                return
            if len(parts) >= 5 and parts[:2] == ["api", "tasks"] and parts[3] == "artifacts":
                path = self.service.artifact_path(parts[2], "/".join(parts[4:]))
                payload = path.read_bytes()
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
                self.end_headers()
                self.wfile.write(payload)
                return
            self._json(404, {"error": "route not found"})
        except DemoError as exc:
            self._json(404, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_origin_allowed():
            self._json(403, {"error": "local API accepts loopback hosts and origins only"})
            return
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        try:
            if parts == ["api", "quote"]:
                if self.headers.get("Content-Type", "").lower().startswith("multipart/form-data;"):
                    fields, uploads = self._multipart_body()
                    requested_skills = None
                    requested_workflow = None
                    if "skills" in fields:
                        try:
                            requested_skills = json.loads(fields["skills"])
                        except json.JSONDecodeError as exc:
                            raise ValueError("skills must be a JSON array of skill names") from exc
                        if not isinstance(requested_skills, list) or any(not isinstance(name, str) for name in requested_skills):
                            raise ValueError("skills must be a JSON array of skill names")
                    if "workflow" in fields:
                        try:
                            requested_workflow = json.loads(fields["workflow"])
                        except json.JSONDecodeError as exc:
                            raise ValueError("workflow must be a JSON workflow object") from exc
                        if not isinstance(requested_workflow, dict):
                            raise ValueError("workflow must be a JSON workflow object")
                    job = self.service.create_document_quote(
                        uploads=uploads,
                        prompt=fields.get("prompt", ""),
                        skill_name=fields.get("skill", ""),
                        model=fields.get("model", ""),
                        sample_count=int(fields.get("sampleCount", "0")),
                        validator_count=int(fields.get("validatorCount", "0")),
                        skill_names=requested_skills,
                        workflow=requested_workflow,
                    )
                    self._json(201, job)
                    return
                data = self._body()
                job = self.service.create_quote(
                    invoice_count=int(data.get("invoiceCount", 100)),
                    sample_count=int(data.get("sampleCount", 30)),
                    validator_count=int(data.get("validatorCount", 3)),
                    corrupt_count=int(data.get("corruptCount", 0)),
                    unavailable_validators=[int(value) for value in data.get("unavailableValidators", [])],
                    disagree=bool(data.get("disagree", False)),
                    tamper_after_commit=bool(data.get("tamperAfterCommit", False)),
                    execution_timeout=bool(data.get("executionTimeout", False)),
                )
                self._json(201, job)
                return
            if parts == ["api", "workflows"]:
                data = self._body()
                workflow = data.get("workflow")
                if not isinstance(workflow, dict):
                    raise ValueError("workflow must be a JSON workflow object")
                name = data.get("name")
                if not isinstance(name, str):
                    raise ValueError("workflow name must be text")
                workflow_id = data.get("workflowId")
                if workflow_id is not None and not isinstance(workflow_id, str):
                    raise ValueError("workflowId must be a UUID string")
                saved = self.service.save_workflow(name, workflow, workflow_id)
                self._json(201 if workflow_id is None else 200, saved)
                return
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "accept":
                self._json(202, self.service.accept_quote(parts[2]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "rating":
                data = self._body()
                self._json(201, self.service.add_rating(parts[2], int(data["stars"]), str(data.get("comment", ""))))
                return
            self._json(404, {"error": "route not found"})
        except (DemoError, ValueError, KeyError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})


def make_server(host: str = "127.0.0.1", port: int = 8000, root: Path | None = None) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the local development API binds to loopback only")
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = DemoService(root or Path.cwd(), os.environ.get("AMN_OLLAMA_MODEL", "qwen2.5:1.5b"), os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local-only AI Mining Network customer API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.root)
    print(f"Local API listening at http://{args.host}:{args.port}; private job data stays under {server.service.state_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping local API")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
