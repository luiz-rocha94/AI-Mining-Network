import test from "node:test";
import assert from "node:assert/strict";
import { formatAtomic, validateGeneralTaskForm, validateTaskForm } from "../src/client.mjs";

test("formats integer atomic currency without floating point", () => {
  assert.equal(formatAtomic(1_234_050, 6), "1.23405");
  assert.equal(formatAtomic(1_000_000, 6), "1");
  assert.equal(formatAtomic(0, 6), "0");
});

test("validates sampling and validator constraints", () => {
  assert.equal(validateTaskForm({ invoiceCount: 100, sampleCount: 30, validatorCount: 3 }), null);
  assert.match(validateTaskForm({ invoiceCount: 10, sampleCount: 11, validatorCount: 3 }), /Samples/);
  assert.match(validateTaskForm({ invoiceCount: 20, sampleCount: 4, validatorCount: 5 }), /Validators/);
});

test("validates general customer tasks", () => {
  const valid = { fileCount: 4, sampleCount: 3, validatorCount: 2, prompt: "Summarize the files", model: "qwen3:4b-instruct", skill: "document-assistant" };
  assert.equal(validateGeneralTaskForm(valid), null);
  assert.equal(validateGeneralTaskForm({ ...valid, skills: ["document-assistant", "invoice-extractor"] }), null);
  assert.match(validateGeneralTaskForm({ ...valid, skills: [] }), /Select at least one/);
  assert.match(validateGeneralTaskForm({ ...valid, skills: ["document-assistant", "document-assistant"] }), /different skills/);
  assert.equal(validateGeneralTaskForm({ ...valid, fileCount: 0, sampleCount: 1, validatorCount: 1 }), null);
  assert.match(validateGeneralTaskForm({ ...valid, prompt: " " }), /prompt/);
  assert.match(validateGeneralTaskForm({ ...valid, sampleCount: 5 }), /samples/);
  assert.match(validateGeneralTaskForm({ ...valid, validatorCount: 4 }), /Validators/);
  assert.match(validateGeneralTaskForm({ ...valid, model: "" }), /Ollama model/);
});
