export function formatBytes(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

export function legacyFormatBytes(n: number): string {
  return `${n}b`;
}
