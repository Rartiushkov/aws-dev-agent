const FIREBASE_AUTH_ORIGIN = 'https://availabl-1f709.firebaseapp.com';

export async function onRequest(context) {
  const { request, params } = context;
  const suffix = params.path ? `/${params.path}` : '';
  const targetUrl = new URL(`/__/auth${suffix}${new URL(request.url).search}`, FIREBASE_AUTH_ORIGIN);

  const proxyRequest = new Request(targetUrl.toString(), request);
  proxyRequest.headers.set('host', new URL(FIREBASE_AUTH_ORIGIN).host);

  return fetch(proxyRequest);
}
