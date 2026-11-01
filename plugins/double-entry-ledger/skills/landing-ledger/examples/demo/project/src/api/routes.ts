import { queryAudit } from '../audit/log';
import { openDocument } from '../format/loader';
import { createNote } from '../notes/create';
import { importNotes } from '../notes/import';
import { deleteNotebook, renameNotebook } from '../notes/notebooks';
import { createShareLink, openShareLink, deleteShareLink } from '../share/links';
import { formatBytes } from '../util/format';
import { deliverWebhook, receiveWebhook } from '../webhooks/deliver';
import { listIntegrationTokens } from '../webhooks/tokens';
import { checkPlanLimit } from './limits';

export function registerRoutes(app: any) {
  app.use(async (req: any, _res: any, next: any) => {
    await checkPlanLimit(req.userId);
    next();
  });
  app.get('/tokens', (req: any) => listIntegrationTokens(req.userId));
  app.post('/notes', (req: any) => createNote(req.userId, req.body.text));
  app.post('/notes/import', (req: any) => importNotes(req.userId, req.body.items));
  app.patch('/notebooks/:id', (req: any) => renameNotebook(req.userId, req.params.id, req.body.name));
  app.delete('/notebooks/:id', (req: any) => deleteNotebook(req.userId, req.params.id));
  app.get('/audit', (req: any) => queryAudit(req.userId));
  app.post('/share', (req: any) => createShareLink(req.body.noteId));
  app.get('/s/:token', (req: any) => openShareLink(req.params.token));
  app.delete('/s/:token', (req: any) => deleteShareLink(req.params.token));
  app.post('/docs/open', (req: any) => openDocument(req.body));
  app.post('/hooks/test', (req: any) => deliverWebhook(req.body.url, { type: 'ping' }));
  app.post('/hooks/incoming', (req: any) => receiveWebhook(req));
  app.get('/size/:n', (req: any) => formatBytes(Number(req.params.n)));
}
