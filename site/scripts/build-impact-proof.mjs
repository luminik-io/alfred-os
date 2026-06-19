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
const AGENT_BRANCH_PREFIXES = csvEnv(
  "ALFRED_IMPACT_AGENT_BRANCH_PREFIXES",
  [
    "alfred/",
    "alfred-nightly/",
    "automerge/",
    "bane/",
    "batman/",
    "damian/",
    "lucius/",
    "nightwing/",
    "rasalghul/",
    "robin/",
  ],
  { lowercase: false },
);
const AGENT_SHIPPED_LABELS = csvEnv("ALFRED_IMPACT_AGENT_LABELS", [
  "agent:authored",
  "agent:done",
  "agent:shipped",
  "alfred:shipped",
  "shipped-by-alfred",
]);
const EXCLUDED_AUTHORS = new Set(
  csvEnv("ALFRED_IMPACT_EXCLUDED_AUTHORS", [
    "app/dependabot",
    "dependabot",
    "dependabot[bot]",
  ]),
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
  .filter(
    (item) =>
      item.__typename === "PullRequest" &&
      item.mergedAt &&
      isWithinWindow(item.mergedAt),
  )
  .sort((a, b) => new Date(b.mergedAt) - new Date(a.mergedAt));

const sortedIssuesOpened = issuesOpened
  .filter(
    (item) =>
      item.__typename === "Issue" &&
      item.createdAt &&
      isWithinWindow(item.createdAt),
  )
  .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));

const sortedIssuesClosed = issuesClosed
  .filter(
    (item) =>
      item.__typename === "Issue" &&
      item.closedAt &&
      isWithinWindow(item.closedAt),
  )
  .sort((a, b) => new Date(b.closedAt) - new Date(a.closedAt));

const agentPrs = sortedPrs.filter(isAgentMarked);
const agentIssuesOpened = sortedIssuesOpened.filter(isAgentIssue);
const agentIssuesClosed = sortedIssuesClosed.filter(isAgentIssue);

const summary = {
  prs_merged: agentPrs.length,
  issues_opened: agentIssuesOpened.length,
  issues_closed: agentIssuesClosed.length,
  issues_triaged: agentIssuesOpened.filter(isTriagedIssue).length,
  lines_added: sum(agentPrs, "additions"),
  lines_removed: sum(agentPrs, "deletions"),
  files_changed: sum(agentPrs, "changedFiles"),
  repo_activity: {
    prs_merged: sortedPrs.length,
    issues_opened: sortedIssuesOpened.length,
    issues_closed: sortedIssuesClosed.length,
    lines_added: sum(sortedPrs, "additions"),
    lines_removed: sum(sortedPrs, "deletions"),
    files_changed: sum(sortedPrs, "changedFiles"),
  },
};

const proof = {
  generated_at: now.toISOString(),
  source: {
    repo: REPO,
    url: `https://github.com/${REPO}`,
    note: "Public Alfred activity from GitHub. PRs require an Alfred branch prefix or shipped label, and Dependabot is excluded. Issues require an agent:* label. The committed JSON is a seed; main-branch site builds refresh it before deploy.",
  },
  window: {
    days: DAYS,
    from: from.toISOString(),
    to: now.toISOString(),
  },
  summary,
  trend: buildTrend(agentPrs),
  prs: agentPrs.slice(0, 10).map((pr) => ({
    number: pr.number,
    title: pr.title,
    url: pr.url,
    merged_at: pr.mergedAt,
    lines_added: pr.additions || 0,
    lines_removed: pr.deletions || 0,
    files_changed: pr.changedFiles || 0,
    agent_authored: isAgentMarked(pr),
  })),
  issues: agentIssuesOpened.slice(0, 8).map((issue) => ({
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
  `Wrote ${OUT}: ${summary.prs_merged} agent PRs, ${summary.issues_opened} agent issues, ${summary.repo_activity.prs_merged} total public PRs.`,
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
              author { login }
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
  const maxAttempts = 3;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
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
      if (response.ok && !payload.errors) {
        return payload.data;
      }
      if (attempt === maxAttempts || response.status < 500) {
        throw new Error(JSON.stringify(payload.errors || payload, null, 2));
      }
    } catch (error) {
      if (attempt === maxAttempts) {
        throw error;
      }
    }
    await sleep(500 * attempt);
  }
  throw new Error("GitHub GraphQL request failed");
}

function sleep(ms) {
  return new Promise((resolveSleep) => {
    setTimeout(resolveSleep, ms);
  });
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

function isWithinWindow(value) {
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) && timestamp >= from.getTime() && timestamp <= now.getTime();
}

function csvEnv(name, fallback, { lowercase = true } = {}) {
  const normalize = (value) => (lowercase ? value.toLowerCase() : value);
  const raw = (process.env[name] || "").trim();
  if (!raw) return fallback.map((item) => normalize(item));
  return raw
    .split(",")
    .map((item) => normalize(item.trim()))
    .filter(Boolean);
}

function labelNames(item) {
  return (item.labels?.nodes || []).map((label) => String(label.name || "").toLowerCase());
}

function authorLogin(item) {
  return String(item.author?.login || "").trim().toLowerCase();
}

function isAgentMarked(item) {
  if (EXCLUDED_AUTHORS.has(authorLogin(item))) {
    return false;
  }
  const labels = labelNames(item);
  const branch = String(item.headRefName || "").trim();
  return (
    AGENT_BRANCH_PREFIXES.some((prefix) => branch.startsWith(prefix)) ||
    labels.some((label) => AGENT_SHIPPED_LABELS.includes(label))
  );
}

function isAgentIssue(issue) {
  return labelNames(issue).some((label) => label.startsWith("agent:"));
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
