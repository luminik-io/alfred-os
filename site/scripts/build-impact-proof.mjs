import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO = "luminik-io/alfred-os";
const DAYS = Number.parseInt(process.env.ALFRED_IMPACT_DAYS || "30", 10);
const OUT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../src/data/impact-proof.json",
);

const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN || readGhToken();

if (!token) {
  throw new Error(
    "Missing GITHUB_TOKEN or GH_TOKEN. Run `gh auth login`, or set a token with public repo read access.",
  );
}

const now = new Date();
const from = new Date(now.getTime() - DAYS * 24 * 60 * 60 * 1000);
const dateOnly = (date) => date.toISOString().slice(0, 10);
const toDate = dateOnly(new Date(now.getTime() + 24 * 60 * 60 * 1000));
const windowRange = `${dateOnly(from)}..${toDate}`;

const prQuery = `repo:${REPO} is:pr is:merged merged:${windowRange}`;
const openedIssueQuery = `repo:${REPO} is:issue created:${windowRange}`;
const closedIssueQuery = `repo:${REPO} is:issue closed:${windowRange}`;

const prs = await searchGitHub(prQuery);
const issuesOpened = await searchGitHub(openedIssueQuery);
const issuesClosed = await searchGitHub(closedIssueQuery);

const sortedPrs = prs
  .filter((item) => item.__typename === "PullRequest" && item.mergedAt)
  .sort((a, b) => new Date(b.mergedAt) - new Date(a.mergedAt));

const sortedIssuesOpened = issuesOpened
  .filter((item) => item.__typename === "Issue")
  .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));

const sortedIssuesClosed = issuesClosed
  .filter((item) => item.__typename === "Issue" && item.closedAt)
  .sort((a, b) => new Date(b.closedAt) - new Date(a.closedAt));

const summary = {
  prs_merged: sortedPrs.length,
  agent_labeled_prs: sortedPrs.filter(isAgentMarked).length,
  issues_opened: sortedIssuesOpened.length,
  issues_closed: sortedIssuesClosed.length,
  issues_triaged: sortedIssuesOpened.filter(isTriagedIssue).length,
  lines_added: sum(sortedPrs, "additions"),
  lines_removed: sum(sortedPrs, "deletions"),
  files_changed: sum(sortedPrs, "changedFiles"),
};

const proof = {
  generated_at: now.toISOString(),
  source: {
    repo: REPO,
    url: `https://github.com/${REPO}`,
    note: "Rolling public GitHub activity for Alfred OS. Generated from merged PRs and issues in the public repository.",
  },
  window: {
    days: DAYS,
    from: from.toISOString(),
    to: now.toISOString(),
  },
  summary,
  trend: buildTrend(sortedPrs),
  prs: sortedPrs.slice(0, 10).map((pr) => ({
    number: pr.number,
    title: pr.title,
    url: pr.url,
    merged_at: pr.mergedAt,
    lines_added: pr.additions || 0,
    lines_removed: pr.deletions || 0,
    files_changed: pr.changedFiles || 0,
    agent_authored: isAgentMarked(pr),
  })),
  issues: sortedIssuesOpened.slice(0, 8).map((issue) => ({
    number: issue.number,
    title: issue.title,
    url: issue.url,
    state: issue.state,
    created_at: issue.createdAt,
    closed_at: issue.closedAt || null,
  })),
};

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, `${JSON.stringify(proof, null, 2)}\n`);
console.log(
  `Wrote ${OUT}: ${summary.prs_merged} PRs, ${summary.issues_opened} opened issues, ${summary.issues_closed} closed issues.`,
);

async function searchGitHub(query) {
  const out = [];
  let cursor = null;
  do {
    const data = await graphQL(
      `query ImpactProof($query: String!, $cursor: String) {
        search(type: ISSUE, query: $query, first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            __typename
            ... on PullRequest {
              number
              title
              url
              mergedAt
              additions
              deletions
              changedFiles
              headRefName
              labels(first: 30) { nodes { name } }
            }
            ... on Issue {
              number
              title
              url
              createdAt
              closedAt
              state
              labels(first: 30) { nodes { name } }
            }
          }
        }
      }`,
      { query, cursor },
    );
    const search = data.search;
    out.push(...search.nodes.filter(Boolean));
    cursor = search.pageInfo.hasNextPage ? search.pageInfo.endCursor : null;
  } while (cursor);
  return out;
}

async function graphQL(query, variables) {
  const response = await fetch("https://api.github.com/graphql", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "User-Agent": "alfred-os-impact-proof",
    },
    body: JSON.stringify({ query, variables }),
  });
  const payload = await response.json();
  if (!response.ok || payload.errors) {
    throw new Error(JSON.stringify(payload.errors || payload, null, 2));
  }
  return payload.data;
}

function readGhToken() {
  try {
    return execFileSync("gh", ["auth", "token"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

function sum(items, field) {
  return items.reduce((total, item) => total + Number(item[field] || 0), 0);
}

function labelNames(item) {
  return (item.labels?.nodes || []).map((label) => label.name);
}

function isAgentMarked(item) {
  return (
    labelNames(item).includes("agent:authored") ||
    String(item.headRefName || "").startsWith("agent/")
  );
}

function isTriagedIssue(issue) {
  return labelNames(issue).some(
    (label) =>
      label.startsWith("agent:") ||
      ["bug", "enhancement", "documentation", "question"].includes(label),
  );
}

function buildTrend(items) {
  const weeks = new Map();
  for (const item of items) {
    const week = isoWeek(item.mergedAt);
    weeks.set(week, (weeks.get(week) || 0) + 1);
  }
  return [...weeks.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([week, prs_merged]) => ({ week, prs_merged }));
}

function isoWeek(value) {
  const input = new Date(value);
  const date = new Date(Date.UTC(input.getUTCFullYear(), input.getUTCMonth(), input.getUTCDate()));
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((date - yearStart) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}
