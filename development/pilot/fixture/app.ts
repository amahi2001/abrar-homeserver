export function greeting(name: string): string {
  return `Saved ${name.trim() || "stranger"}`;
}

if (typeof document !== "undefined") {
  document.querySelector("button")!.addEventListener("click", () => {
    document.querySelector("[role=status]")!.textContent = greeting(document.querySelector("input")!.value);
  });
}
