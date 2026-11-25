import http from './request'

// 代理连通性测试（后端并发测试，可能耗时，单独放宽超时到 3 分钟）
export const testProxies = (proxies, timeout = 8) =>
  http.post('/api/proxy/test', { proxies, timeout }, { timeout: 180000 })

export const fetchZooproxy = (api_url, scheme = 'http') =>
  http.post('/api/proxy/zooproxy/fetch', { api_url, scheme }, { timeout: 60000 })
