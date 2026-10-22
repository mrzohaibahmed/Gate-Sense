export type Theme = "light" | "dark" | "system";
const KEY = "gatesense.theme";

export function storedTheme(): Theme {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Put the "dark" class on <html> for dark, or for "system" when the OS prefers dark. */
export function applyTheme(theme: Theme = storedTheme()): void {
  const dark = theme === "dark" || (theme === "system" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", !!dark);
}

export function setTheme(theme: Theme): void {
  try {
    if (theme === "system") window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, theme);
  } catch {
    /* not remembered */
  }
  applyTheme(theme);
}
