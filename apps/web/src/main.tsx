import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import { App } from '@/App';
import '@/index.css';

const container = document.getElementById('root');

// Fail loudly rather than silently rendering nothing (engineering rule 6).
if (!container) {
  throw new Error('Mount point #root is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
