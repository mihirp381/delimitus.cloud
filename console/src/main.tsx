import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { DEV_LOGIN } from './auth/flags';
import { createSession } from './auth/session';
import { App, createConsole } from './router';
import './tokens/tokens.css';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('index.html has no #root');

const session = createSession(DEV_LOGIN ? window.sessionStorage : null);
const app = createConsole({ baseUrl: window.location.origin, session });

createRoot(root).render(
  <StrictMode>
    <App console={app} />
  </StrictMode>,
);
