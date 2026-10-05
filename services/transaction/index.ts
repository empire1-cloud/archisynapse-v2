// Entry point expected by package.json (main, start, dev) and the Dockerfile
// (node dist/index.js). The service itself lives in transaction-service-index.ts;
// before this file existed, dist/index.js was never built and the container
// could not start.
import './transaction-service-index';
