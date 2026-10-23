import { PeerTransportError } from "./protocol.mjs";

export function encodeFrame(value, maxBytes) {
  const body = Buffer.from(JSON.stringify(value), "utf8");
  if (body.byteLength > maxBytes) {
    throw new PeerTransportError("MESSAGE_TOO_LARGE", `direct frame exceeds ${maxBytes} byte limit`);
  }
  const frame = Buffer.allocUnsafe(body.byteLength + 4);
  frame.writeUInt32BE(body.byteLength, 0);
  body.copy(frame, 4);
  return frame;
}

export async function readFrame(stream, maxBytes) {
  let frame = Buffer.alloc(0);
  for await (const chunk of stream) {
    const bytes = Buffer.from(chunk instanceof Uint8Array ? chunk : chunk.subarray());
    frame = Buffer.concat([frame, bytes]);
    if (frame.byteLength > maxBytes + 4) {
      const error = new PeerTransportError("MESSAGE_TOO_LARGE", "direct frame exceeds configured limit");
      stream.abort?.(error);
      throw error;
    }
    if (frame.byteLength < 4) continue;
    const expectedBytes = frame.readUInt32BE(0);
    if (expectedBytes > maxBytes) throw new PeerTransportError("MESSAGE_TOO_LARGE", "direct frame exceeds configured limit");
    if (frame.byteLength < expectedBytes + 4) continue;
    if (frame.byteLength > expectedBytes + 4) {
      throw new PeerTransportError("INVALID_FRAME", "direct stream contained bytes after its single frame");
    }
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(frame.subarray(4)));
    } catch (error) {
      throw new PeerTransportError("INVALID_FRAME", "direct frame body is not UTF-8 JSON", { cause: error });
    }
  }
  throw new PeerTransportError("INVALID_FRAME", "direct stream ended before a complete frame arrived");
}

export async function writeFrame(stream, value, maxBytes) {
  const accepted = stream.send(encodeFrame(value, maxBytes));
  if (!accepted) await stream.onDrain();
}
