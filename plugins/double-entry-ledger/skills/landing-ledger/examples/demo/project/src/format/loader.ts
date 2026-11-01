import { layoutFromV1 } from './upgrade';

export function openDocument(raw: any) {
  if (raw.formatVersion === 1) {
    return layoutFromV1(raw);
  }
  return raw;
}
