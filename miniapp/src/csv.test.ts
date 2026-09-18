import { expect, it } from "vitest";
import { parseCsv } from "./csv";

it("keeps commas and newlines inside quoted cells", () => {
  expect(parseCsv('name,note\r\nBread,"fresh, daily"\r\nTea,"hot\nwater"')).toEqual([["name", "note"], ["Bread", "fresh, daily"], ["Tea", "hot\nwater"]]);
});
it("detects separators outside quotes and honours a TSV extension", () => {
  expect(parseCsv('"a,b,c"\tscore\nx\t2')).toEqual([["a,b,c", "score"], ["x", "2"]]);
  expect(parseCsv('a,b\nc,d', 2000, "\t")).toEqual([["a,b"], ["c,d"]]);
});
it("bounds the row count without losing escaped quotes or final empty cells", () => {
  expect(parseCsv('a,b\n"say ""hi""",\nx,y', 2)).toEqual([["a", "b"], ['say "hi"', ""]]);
});
