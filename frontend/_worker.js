const FIREBASE_AUTH_ORIGIN = 'https://availabl-1f709.firebaseapp.com';

function shouldProxy(pathname) {
  return pathname.startsWith('/__/auth/') || pathname === '/__/firebase/init.json';
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (shouldProxy(url.pathname)) {
      const targetUrl = new URL(`${url.pathname}${url.search}`, FIREBASE_AUTH_ORIGIN);
      const proxyRequest = new Request(targetUrl.toString(), request);
      proxyRequest.headers.set('host', new URL(FIREBASE_AUTH_ORIGIN).host);
      return fetch(proxyRequest);
    }

    return env.ASSETS.fetch(request);
  },
};
