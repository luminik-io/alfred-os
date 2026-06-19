import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const DAYS = Number.parseInt(process.env.ALFRED_PRODUCT_PROOF_DAYS || "30", 10);
const OUT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../src/data/luminik-product-proof.json",
);
const REPOS = csvEnv("ALFRED_PRODUCT_PROOF_REPOS", []);
const AGENT_BRANCH_PREFIXES = csvEnv(
  "ALFRED_PRODUCT_PROOF_AGENT_BRANCH_PREFIXES",
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
const AGENT_LABELS = csvEnv("ALFRED_PRODUCT_PROOF_AGENT_LABELS", [
  "agent:authored",
  "agent:done",
  "agent:shipped",
  "alfred:shipped",
  "shipped-by-alfred",
]);
const EXCLUDED_AUTHORS = new Set(
  csvEnv("ALFRED_PRODUCT_PROOF_EXCLUDED_AUTHORS", [
    "app/dependabot",
    "dependabot",
    "dependabot[bot]",
  ]),
);

if (REPOS.length === 0) {
  throw new Error(
    "Set ALFRED_PRODUCT_PROOF_REPOS to a comma-separated list of repo slugs. The output stays aggregate-only.",
  );
}

const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN || readGhToken();

if (!token) {
  throw new Error(
    "Missing GITHUB_TOKEN or GH_TOKEN. Run `gh auth login`, or set a token with repo read access.",
  );
}

const now = new Date();
const from = new Date(now.getTime() - DAYS * 24 * 60 * 60 * 1000);
const dateOnly = (date) => date.toISOString().slice(0, 10);
const toDate = dateOnly(new Date(now.getTime() + 24 * 60 * 60 * 1000));
const windowRange = `${dateOnly(from)}..${toDate}`;

const rows = [];

for (const repo of REPOS) {
  const prs = (await searchGitHub(`repo:${repo} is:pr is:merged merged:${windowRange}`)).filter(
    (item) => item.__typename === "PullRequest",
  );
  const openedIssues = (await searchGitHub(`repo:${repo} is:issue created:${windowRange}`)).filter(
    (item) => item.__typename === "Issue",
  );
  const closedIssues = (await searchGitHub(`repo:${repo} is:issue closed:${windowRange}`)).filter(
    (item) => item.__typename === "Issue",
  );
  const agentPrs = prs.filter(isAgentPr);
  const agentIssuesOpened = openedIssues.filter(isAgentIssue);
  const agentIssuesClosed = closedIssues.filter(isAgentIssue);
  rows.push({
    agent_prs_merged: agentPrs.length,
    agent_issues_opened: agentIssuesOpened.length,
    agent_issues_closed: agentIssuesClosed.length,
    lines_added: sum(agentPrs, "additions"),
    lines_removed: sum(agentPrs, "deletions"),
    files_changed: sum(agentPrs, "changedFiles"),
    total_prs_merged: prs.length,
    total_issues_opened: openedIssues.length,
    total_issues_closed: closedIssues.length,
  });
  console.error(`${repo}: ${agentPrs.length} agent PRs, ${agentIssuesOpened.length} agent issues`);
}

const summary = rows.reduce(
  (acc, row) => {
    for (const key of Object.keys(row)) {
      acc[key] += row[key];
    }
    return acc;
  },
  {
    repos_scanned: REPOS.length,
    agent_prs_merged: 0,
    agent_issues_opened: 0,
    agent_issues_closed: 0,
    lines_added: 0,
    lines_removed: 0,
    files_changed: 0,
    total_prs_merged: 0,
    total_issues_opened: 0,
    total_issues_closed: 0,
  },
);

const proof = {
  generated_at: now.toISOString(),
  source: {
    label: process.env.ALFRED_PRODUCT_PROOF_LABEL || "Luminik product setup",
    note:
      "Aggregate-only snapshot from GitHub data visible to the site build token. " +
      "The source repos stay in Actions config and are not committed here. " +
      "PRs count when they carry an agent label or known Alfred branch prefix. " +
      "Issues count when they carry an agent:* label.",
  },
  window: {
    days: DAYS,
    from: from.toISOString(),
    to: now.toISOString(),
  },
  summary,
};

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, `${JSON.stringify(proof, null, 2)}\n`);
console.log(
  `Wrote ${OUT}: ${summary.agent_prs_merged} agent PRs, ${summary.agent_issues_opened} agent issues, ${summary.repos_scanned} repos.`,
);

async function searchGitHub(query) {
  const out = [];
  let cursor = null;
  do {
    const data = await graphQL(
      `query ProductProof($query: String!, $cursor: String) {
        search(type: ISSUE, query: $query, first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            __typename
            ... on PullRequest {
              additions
              author { login }
              changedFiles
              deletions
              headRefName
              labels(first: 50) { nodes { name } }
            }
            ... on Issue {
              labels(first: 50) { nodes { name } }
            }
          }
        }
      }`,
      { query, cursor },
    );
    out.push(...data.search.nodes.filter(Boolean));
    cursor = data.search.pageInfo.hasNextPage ? data.search.pageInfo.endCursor : null;
  } while (cursor);
  return out;
}

async function graphQL(query, variables) {
  const maxAttempts = 3;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    let response;
    let payload;
    try {
      response = await fetch("https://api.github.com/graphql", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "User-Agent": "alfred-product-proof",
        },
        body: JSON.stringify({ query, variables }),
      });
      payload = await response.json();
    } catch (error) {
      if (attempt === maxAttempts) {
        throw error;
      }
      await sleep(500 * attempt);
      continue;
    }
    if (response.ok && !payload.errors) {
      return payload.data;
    }
    const message = JSON.stringify(payload.errors || payload, null, 2);
    if (payload.errors || response.status < 500 || attempt === maxAttempts) {
      throw new Error(message);
    }
    await sleep(500 * attempt);
  }
  throw new Error("GitHub GraphQL request failed");
}

function labelNames(item) {
  return (item.labels?.nodes || []).map((label) => String(label.name || "").toLowerCase());
}

function isAgentPr(pr) {
  if (EXCLUDED_AUTHORS.has(String(pr.author?.login || "").trim().toLowerCase())) {
    return false;
  }
  const branch = String(pr.headRefName || "");
  const labels = labelNames(pr);
  return (
    AGENT_BRANCH_PREFIXES.some((prefix) => branch.startsWith(prefix)) ||
    labels.some((label) => label.startsWith("agent:") || AGENT_LABELS.includes(label))
  );
}

function isAgentIssue(issue) {
  return labelNames(issue).some((label) => label.startsWith("agent:"));
}

function sum(items, field) {
  return items.reduce((total, item) => total + Number(item[field] || 0), 0);
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
