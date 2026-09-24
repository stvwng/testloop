import { describe, expect, it } from "vitest";
import { add, divide, slugify } from "./calc";

const seen: string[] = [];

describe("calc", () => {
  it("adds", () => {
    expect(add(2, 3)).toBe(5);
  });

  it("divides with true division", () => {
    expect(divide(7, 2)).toBe(3.5);
  });

  it("throws on divide by zero", () => {
    expect(() => divide(1, 0)).toThrow("cannot divide by zero");
  });

  it("slugify records state", () => {
    seen.push(slugify("Hello World"));
    expect(seen[seen.length - 1]).toBe("hello-world");
  });

  it("depends on the previous test", () => {
    expect(seen).toEqual(["hello-world"]);
  });
});
