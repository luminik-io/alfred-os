import { CheckCircle2, RefreshCw } from "lucide-react";
import { useCallback, useState } from "react";

import { errorDetail, loadSetupRepos, saveSetupRepos } from "../../api";
import type { SetupRepo } from "../../types";
import { Badge, Button, Card, CardContent } from "../ui";
import type { OnboardingNotice } from "./types";

// Split "owner/name" into a prominent short name and a muted full slug, so Maya
// recognises the project by its name, not by parsing an org slug.
function repoShortName(slug: string): string {
  const slash = slug.lastIndexOf("/");
  return slash === -1 ? slug : slug.slice(slash + 1);
}

/**
 * Step 3: Pick repositories. Loads the repo list (GET /api/setup/repos),
 * multi-select leading with the repo NAME and its description (full slug muted),
 * private badge where relevant, and saves the selection (POST /api/setup/repos).
 */
export function ReposStep({
  baseUrl,
  canMutate,
  githubConnected,
  selectedCount,
  onSaved,
  setNotice,
}: {
  baseUrl: string;
  canMutate: boolean;
  githubConnected: boolean;
  selectedCount: number;
  onSaved: () => Promise<void>;
  setNotice: (notice: OnboardingNotice) => void;
}) {
  const [repos, setRepos] = useState<SetupRepo[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedRepos, setSavedRepos] = useState<string[] | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await loadSetupRepos(baseUrl);
      setRepos(result.repos);
      setPicked(new Set(result.selected.map((r) => r.toLowerCase())));
      setError(result.error || null);
      setLoaded(true);
    } catch (err) {
      setError(errorDetail(err) || "Could not list your repositories.");
    } finally {
      setLoading(false);
    }
  }, [baseUrl]);

  const toggle = (slug: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      const key = slug.toLowerCase();
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const save = async () => {
    setSaving(true);
    try {
      const visible = new Map(
        repos.map((repo) => [repo.name_with_owner.toLowerCase(), repo.name_with_owner] as const),
      );
      const selected = Array.from(picked).map((slug) => visible.get(slug) || slug);
      const result = await saveSetupRepos(baseUrl, selected);
      setSavedRepos(result.repos);
      setNotice({
        tone: "ok",
        message: `Saved ${result.repos.length} ${
          result.repos.length === 1 ? "repository" : "repositories"
        } Alfred can work in.`,
      });
      await onSaved();
    } catch (err) {
      setNotice({
        tone: "error",
        message: errorDetail(err) || "Could not save your repository selection.",
      });
    } finally {
      setSaving(false);
    }
  };

  if (!githubConnected) {
    return (
      <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
        <CardContent className="px-3 text-sm text-muted-foreground">
          Connect GitHub first (the previous step). Once you are signed in, your repositories appear
          here to choose from.
        </CardContent>
      </Card>
    );
  }

  const pickedLabel = `${picked.size} ${picked.size === 1 ? "repository" : "repositories"}`;

  return (
    <div className="grid gap-3">
      {!loaded ? (
        <Button
          variant="outline"
          className="w-fit"
          type="button"
          onClick={() => void load()}
          disabled={loading}
        >
          <RefreshCw size={14} aria-hidden="true" className={loading ? "animate-spin" : undefined} />
          <span>{loading ? "Loading repositories" : "Load my repositories"}</span>
        </Button>
      ) : null}

      {error ? (
        <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
          <CardContent className="px-3 text-sm text-muted-foreground">{error}</CardContent>
        </Card>
      ) : null}

      {loaded && !error ? (
        repos.length ? (
          <>
            <div
              className="grid max-h-[42vh] gap-2 overflow-y-auto pr-1"
              role="group"
              aria-label="Repositories Alfred may work in"
            >
              {repos.map((repo) => (
                <label
                  className="grid cursor-pointer grid-cols-[auto_1fr_auto] gap-2 rounded-lg border border-border/70 bg-background/55 px-3 py-2 transition-colors hover:bg-muted/45"
                  key={repo.name_with_owner}
                >
                  <input
                    className="mt-1 size-4 accent-primary"
                    type="checkbox"
                    checked={picked.has(repo.name_with_owner.toLowerCase())}
                    onChange={() => toggle(repo.name_with_owner)}
                  />
                  <span className="grid min-w-0 gap-0.5">
                    <span className="truncate text-sm font-medium text-foreground">
                      {repoShortName(repo.name_with_owner)}
                    </span>
                    {repo.description ? (
                      <span className="line-clamp-2 text-xs text-muted-foreground">
                        {repo.description}
                      </span>
                    ) : null}
                    <span className="truncate font-mono text-[0.7rem] text-muted-foreground/80">
                      {repo.name_with_owner}
                    </span>
                  </span>
                  <span className="flex flex-wrap justify-end gap-1">
                    {repo.is_private ? <Badge variant="outline">private</Badge> : null}
                    {repo.listed === false ? <Badge variant="secondary">saved</Badge> : null}
                  </span>
                </label>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button type="button" onClick={() => void save()} disabled={!canMutate || saving}>
                <CheckCircle2 size={15} aria-hidden="true" />
                <span>{saving ? "Saving" : `Save ${pickedLabel}`}</span>
              </Button>
              <Button variant="outline" type="button" onClick={() => void load()} disabled={loading}>
                <RefreshCw size={14} aria-hidden="true" />
                <span>Refresh</span>
              </Button>
            </div>
          </>
        ) : (
          <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
            <CardContent className="px-3 text-sm text-muted-foreground">
              <strong className="block text-foreground">No repositories found.</strong>
              gh did not return any repositories for your account.
            </CardContent>
          </Card>
        )
      ) : null}

      {savedRepos ? (
        <p className="text-sm text-muted-foreground">
          Alfred is now scoped to:{" "}
          {savedRepos.length ? savedRepos.join(", ") : "no repositories"}.
        </p>
      ) : selectedCount ? (
        <p className="text-sm text-muted-foreground">
          {selectedCount} {selectedCount === 1 ? "repository" : "repositories"} selected. Load the
          list to change them.
        </p>
      ) : null}

      {!canMutate ? (
        <p className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
          The desktop app saves repository choices. The browser preview can load the list but cannot
          save.
        </p>
      ) : null}
    </div>
  );
}
