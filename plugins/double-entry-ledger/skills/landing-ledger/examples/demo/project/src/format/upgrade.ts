export function layoutFromV1(doc: any) {
  return { ...doc, blocks: (doc.paragraphs ?? []).map((p: string) => ({ type: 'text', text: p })) };
}
