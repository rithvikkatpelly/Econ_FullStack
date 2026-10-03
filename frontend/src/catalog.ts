import type { Frequency } from "./api/types";

/** The series featured on the landing page. Display copy only — units,
 * titles and real values always come from the API. */
export interface CuratedSeries {
  id: string;
  name: string;
  blurb: string;
  /** Frequency to request for the hero snapshot. FRED only lets you *lower* a
   * series' frequency, so quarterly GDP must be asked for quarterly. */
  freq: Frequency;
}

export const CURATED: CuratedSeries[] = [
  { id: "UNRATE", name: "Unemployment rate", blurb: "Share of the labor force without a job", freq: "m" },
  { id: "CPIAUCSL", name: "Consumer prices (CPI)", blurb: "What urban households pay, all items", freq: "m" },
  { id: "CPILFESL", name: "Core CPI", blurb: "CPI without food and energy", freq: "m" },
  { id: "PCEPILFE", name: "Core PCE", blurb: "The Fed's preferred inflation gauge", freq: "m" },
  { id: "FEDFUNDS", name: "Fed funds rate", blurb: "The Fed's overnight policy rate", freq: "m" },
  { id: "DGS10", name: "10-year Treasury yield", blurb: "What Washington pays to borrow for a decade", freq: "m" },
  { id: "GDP", name: "Gross domestic product", blurb: "Total US output, per quarter", freq: "q" },
];

export const CURATED_BY_ID: Record<string, CuratedSeries> = Object.fromEntries(
  CURATED.map((s) => [s.id, s]),
);

export interface Comparison {
  title: string;
  blurb: string;
  ids: string[];
}

export const COMPARISONS: Comparison[] = [
  { title: "Jobs vs. prices", blurb: "Does a tight labor market show up in inflation?", ids: ["UNRATE", "CPIAUCSL"] },
  { title: "Policy rate vs. 10-year yield", blurb: "How far the Fed's rate pulls long-term borrowing costs", ids: ["FEDFUNDS", "DGS10"] },
  { title: "Three inflation gauges", blurb: "Headline CPI, core CPI and core PCE side by side", ids: ["CPIAUCSL", "CPILFESL", "PCEPILFE"] },
];

/** Plain-language prompts for the "Ask about" marquee and the search box. */
export const TOPIC_ROWS: string[][] = [
  ["unemployment rate", "core inflation", "10 year treasury yield", "fed funds rate", "gdp", "consumer prices", "jobs"],
  ["cost of living", "economic growth", "borrowing", "the yield curve", "labor market", "monetary policy", "rate hikes"],
];

export const REPO_URL = "https://github.com/rithvikkatpelly/Econ_FullStack";
