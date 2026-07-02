// ─── Imports ─────────────────────────────────────────────────────────────────
import { initializeApp, getApps } from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js';
import {
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  signOut,
  onAuthStateChanged,
} from 'https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js';

// ─── Firebase config ─────────────────────────────────────────────────────────
const FIREBASE_CONFIG = {
  apiKey:            "AIzaSyC2s8vy7THhcs9YO5Ro5lwenICXZpzmgD8",
  authDomain:        "availabl-1f709.firebaseapp.com",
  projectId:         "availabl-1f709",
  storageBucket:     "availabl-1f709.firebasestorage.app",
  messagingSenderId: "25354908364",
  appId:             "1:25354908364:web:7932be5f9c684d6862c869",
  measurementId:     "G-X54ZN6HNWE",
};

const app      = getApps().length ? getApps()[0] : initializeApp(FIREBASE_CONFIG);
const auth     = getAuth(app);
const provider = new GoogleAuthProvider();
provider.setCustomParameters({ prompt: 'select_account' });

// ─── Sign in ────────────────────────────────────────────────────────────────
export async function signInWithGoogle() {
  const result = await signInWithPopup(auth, provider);
  return result.user;
}

// ─── Sign out ───────────────────────────────────────────────────────────────
export async function signOutUser() {
  await signOut(auth);
  window.location.replace('/index.html');
}

// ─── Get current user (cached) ──────────────────────────────────────────────
export function getCurrentUser() {
  return auth.currentUser;
}

// ─── Get Firebase ID token for backend requests ──────────────────────────────
export async function getIdToken() {
  const user = auth.currentUser;
  if (!user) throw new Error('Not authenticated');
  return user.getIdToken();
}

// ─── Guard: redirect to login if not signed in ──────────────────────────────
// Call this at the top of any protected page (dashboard, migrations, etc.)
export function requireAuth(redirectTo = './login.html') {
  return new Promise((resolve) => {
    const unsub = onAuthStateChanged(auth, (user) => {
      unsub();
      if (!user) {
        window.location.replace(redirectTo);
      } else {
        resolve(user);
      }
    });
  });
}

// ─── onAuthStateChanged helper ──────────────────────────────────────────────
export function watchAuth(callback) {
  return onAuthStateChanged(auth, callback);
}
