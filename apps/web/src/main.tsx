import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import { configureApi } from './api/client';
import './styles/global.css';

void configureApi().finally(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
});
