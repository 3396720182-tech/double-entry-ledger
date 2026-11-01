// 运维脚本：列出没有任何笔记引用的标签，人工确认后再删
import { listUnusedTags } from '../src/notes/tags';

listUnusedTags().then((rows: any[]) => console.log(`${rows.length} unused tags`));
