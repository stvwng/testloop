export function add(a: number, b: number): number {
  return a + b;
}

export function divide(a: number, b: number): number {
  if (b === 0) throw new Error("cannot divide by zero");
  // BUG: truncates; tests expect true division.
  return Math.trunc(a / b);
}

export function slugify(text: string): string {
  return text.toLowerCase().split(/\s+/).join("-");
}
