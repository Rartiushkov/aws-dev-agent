import { watchAuth } from './auth.js';

const API = 'https://availabl-backend.onrender.com';

function getAuthenticatedUser() {
  return new Promise((resolve) => {
    const unsub = watchAuth((user) => {
      unsub();
      resolve(user);
    });
  });
}

export async function startCheckout(plan = 'pro') {
  const user = await getAuthenticatedUser();
  if (!user) { window.location.href = '/login.html'; return; }

  try {
    const token = await user.getIdToken();
    const email = encodeURIComponent(user.email || '');
    const res = await fetch(`${API}/api/checkout?plan=${plan}&email=${email}`, {
      headers: { 'Authorization': `Bearer ${token}` },
    });
    const data = await res.json();
    if (data.url) {
      window.location.href = data.url;
    } else {
      alert('Could not start checkout: ' + (data.error || 'Unknown error'));
    }
  } catch(e) {
    alert('Error: ' + e.message);
  }
}
