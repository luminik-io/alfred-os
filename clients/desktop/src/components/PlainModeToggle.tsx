import { Switch } from "./ui";

// A visible in-app toggle for plain mode. Seeded from the server's
// ALFRED_INTAKE_PROFILE default, it lets a non-developer flip jargon-free
// coaching on/off; the value rides each converse call as `plain`. Rendered as a
// labelled Radix switch so keyboard, pointer, and screen-reader behavior stays
// native while Alfred owns the visual skin.
export function PlainModeToggle({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="plain-toggle">
      <Switch
        className="plain-toggle__switch"
        checked={checked}
        onCheckedChange={onChange}
        aria-label="Plain language"
      />
      <span className="plain-toggle__label">Plain language</span>
    </label>
  );
}
