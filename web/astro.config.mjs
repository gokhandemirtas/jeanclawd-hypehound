import { defineConfig } from 'astro/config';
import { existsSync, readFileSync } from 'fs';
import { join } from 'path';

export default defineConfig({
  output: 'static',
  vite: {
    plugins: [
      {
        // Serve index.html for directory requests in public/ (Cloudflare Pages behaviour in dev)
        name: 'public-dir-index',
        configureServer(server) {
          server.middlewares.use((req, res, next) => {
            const url = req.url?.split('?')[0] ?? '';
            if (url.endsWith('/')) {
              const candidate = join(process.cwd(), 'public', url, 'index.html');
              if (existsSync(candidate)) {
                res.setHeader('Content-Type', 'text/html; charset=utf-8');
                res.end(readFileSync(candidate));
                return;
              }
            }
            next();
          });
        },
      },
    ],
  },
});
