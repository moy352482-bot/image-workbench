// Request transport receives live credentials; it never owns account/UI state.
(() => {
  'use strict';
  function create({ getWorkspaceId, getCsrfToken, onUnauthorized, fetchImpl = (...args) => fetch(...args) }) {
    return async function api(path, options = {}) {
      const headers = new Headers(options.headers || {});
      const workspaceId = getWorkspaceId();
      if (workspaceId && !headers.has('X-Workspace-ID')) headers.set('X-Workspace-ID', workspaceId);
      if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
      if (options.method && options.method !== 'GET') headers.set('X-Workbench-CSRF', getCsrfToken());
      let response;
      try {
        response = await fetchImpl(path, { ...options, headers, cache: 'no-store' });
      } catch (error) {
        throw new Error('无法连接本地工作台，请确认服务仍在运行后重试。');
      }
      if (!response.ok) {
        if (response.status === 401) { onUnauthorized(); throw new Error('请重新登录'); }
        let message = `请求失败（${response.status}）`;
        try {
          const payload = await response.json();
          if (typeof payload.detail === 'string') message = payload.detail;
        } catch (error) { /* Keep the readable status when the response is not JSON. */ }
        throw new Error(message);
      }
      if (response.status === 204) return null;
      return response.json();
    }
  }
  window.WorkbenchApi = Object.freeze({ create });
})();
