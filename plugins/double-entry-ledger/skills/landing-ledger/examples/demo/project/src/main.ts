import { registerRoutes } from './api/routes';

const app: any = (globalThis as any).createApp();
registerRoutes(app);
app.listen(3000);
