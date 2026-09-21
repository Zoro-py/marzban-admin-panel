/* Categorical identity colors for the history charts. Slots 1–2 are the
 * validated Finance palette carried over verbatim (RevenueChart); 3–8 extend
 * it in the same Okabe-Itcho-derived family, stepped per surface. A series is
 * an IDENTITY (which account), never a state — semantic colors (red debt /
 * green paid / amber pending) are never used for accounts. More than 8
 * accounts reuse colors, which the direct lane labels disambiguate. */
export const SERIES_COLORS: { light: string[]; dark: string[] } = {
  light: ['#2a78d6', '#1baf7a', '#b26a00', '#6a51a3', '#b0417a', '#007681', '#8c6d31', '#4a5fc1'],
  dark: ['#3987e5', '#199e70', '#e69f00', '#9e86c8', '#d16ba5', '#2ab5ac', '#b08d57', '#7b8ce0'],
}

export function accountColor(resolved: 'light' | 'dark', index: number): string {
  const slots = SERIES_COLORS[resolved]
  return slots[index % slots.length]!
}
