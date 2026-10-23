"""Optional local MCP tool server for the invoice extraction skill.

Install the ``mcp`` extra to enable this process. It uses the same explicit
local Ollama backend as the customer workflow and has no remote fallback.
"""

from __future__ import annotations

import os

from .invoice import InvoiceFile, OllamaExtractor


def build_server():
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError('Install the optional MCP dependency with: pip install -e ".[mcp]"') from exc

    server = MCPServer("AI Mining Network invoice skill")
    model = os.environ.get("AMN_OLLAMA_MODEL", "qwen2.5:1.5b")
    ollama_url = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

    @server.tool()
    def extract_invoice(invoice_id: str, document_text: str) -> dict:
        """Extract normalized fields from one untrusted invoice text document."""
        if len(document_text) > 100_000:
            raise ValueError("invoice text exceeds the 100 KB limit")
        extractor = OllamaExtractor(model=model, base_url=ollama_url)
        result = extractor.extract_batch([
            InvoiceFile(invoice_id=invoice_id, path="mcp://input", input_hash="sha256:" + "0" * 64, text=document_text)
        ])[0]
        return {
            "invoiceId": result.invoiceId,
            "supplier": result.supplier,
            "invoiceDate": result.invoiceDate,
            "totalAmount": result.totalAmount,
            "currency": result.currency,
            "modelDigest": extractor.model_digest,
            "metrics": extractor.last_metrics,
        }

    return server


def main() -> None:
    server = build_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
