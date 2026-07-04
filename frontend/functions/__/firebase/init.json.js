const FIREBASE_AUTH_ORIGIN = 'https://availabl-1f709.firebaseapp.com';

export async function onRequest(context) {
  const requestUrl = new URL(context.request.url);
  const targetUrl = new URL(`/__/firebase/init.json${requestUrl.search}`, FIREBASE_AUTH_ORIGIN);
  return fetch(targetUrl.toString(), {
    headers: {
      'accept': context.request.headers.get('accept') || 'application/json',
    },
  });
}
