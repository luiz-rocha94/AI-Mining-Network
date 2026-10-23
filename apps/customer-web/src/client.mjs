export function formatAtomic(amount, decimals = 6) {
  const raw = BigInt(amount);
  const scale = 10n ** BigInt(decimals);
  const whole = raw / scale;
  const fraction = String(raw % scale).padStart(decimals, "0").replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : String(whole);
}

export function validateTaskForm({ invoiceCount, sampleCount, validatorCount }) {
  const invoices = Number(invoiceCount);
  const samples = Number(sampleCount);
  const validators = Number(validatorCount);
  if (!Number.isInteger(invoices) || invoices < 1 || invoices > 1000) return "Invoices must be between 1 and 1,000.";
  if (!Number.isInteger(samples) || samples < 1 || samples > invoices) return "Samples must be between 1 and the invoice count.";
  if (!Number.isInteger(validators) || validators < 1 || validators > 10 || validators > samples) return "Validators must be 1–10 and no more than the sample count.";
  return null;
}

export function validateGeneralTaskForm({ fileCount, sampleCount, validatorCount, prompt, model, skill, skills }) {
  const files = Number(fileCount);
  const samples = Number(sampleCount);
  const validators = Number(validatorCount);
  if (!Number.isInteger(files) || files < 0 || files > 100) return "Attach up to 100 supported files.";
  if (!String(prompt ?? "").trim() || String(prompt).length > 20_000) return "Enter a task prompt of 1–20,000 characters.";
  const selectedSkills = Array.isArray(skills) ? skills : [skill];
  if (!selectedSkills.length || selectedSkills.some(name => !String(name ?? "").trim())) return "Select at least one published skill.";
  if (selectedSkills.length > 10 || new Set(selectedSkills).size !== selectedSkills.length) return "Select up to 10 different skills.";
  if (!String(model ?? "").trim()) return "Select an available Ollama model.";
  if (!Number.isInteger(samples) || samples < 1 || samples > Math.max(files, 1)) return "Verification samples must be between 1 and the task unit count.";
  if (!Number.isInteger(validators) || validators < 1 || validators > 10 || validators > samples) return "Validators must be 1–10 and no more than the sample count.";
  return null;
}
