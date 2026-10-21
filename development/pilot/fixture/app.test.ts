import { expect, test } from "bun:test";
import { greeting } from "./app";

test("the form trims names, handles blanks and preserves text literally", () => {
  expect(greeting("  Ada  ")).toBe("Saved Ada");
  expect(greeting("\t ")).toBe("Saved stranger");
  expect(greeting("李 <script>")).toBe("Saved 李 <script>");
});
