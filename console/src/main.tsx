import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { DEV_LOGIN } from './auth/flags';
import { consoleOAuth } from './auth/oauth';
import { createSession } from './auth/session';
import { App, createConsole } from './router';
import './tokens/tokens.css';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('index.html has no #root');

const oauth = consoleOAuth(window.location.origin);
const session = createSession(DEV_LOGIN ? window.sessionStorage : null, oauth);
const app = createConsole({ baseUrl: window.location.origin, session, oauth });

createRoot(root).render(
  <StrictMode>
    <App console={app} />
  </StrictMode>,
);
