// Js/firebase.js
// Earnova Firebase Configuration + Django API Shim
// Firebase Auth handles login; the shim redirects Firestore calls to Django.

const API_BASE_URL = 'https://earnova-kz37.onrender.com/api'; // Change to https://api.earnova.cloud in production

const firebaseConfig = {
  apiKey: "AIzaSyB0sKd2AquQRMCEr3i3Cr7D1vCDbZRcSXM",
  authDomain: "earnova-1ff64.firebaseapp.com",
  databaseURL: "https://earnova-1ff64-default-rtdb.firebaseio.com",
  projectId: "earnova-1ff64",
  storageBucket: "earnova-1ff64.firebasestorage.app",
  messagingSenderId: "532356848803",
  appId: "1:532356848803:web:f94528d77f42a7310cf316",
  measurementId: "G-L8G1NFDQ5B"
};

// Initialize Firebase
if (typeof firebase !== 'undefined') {
  firebase.initializeApp(firebaseConfig);
  console.log('Firebase Auth initialized successfully');
} else {
  console.warn('Firebase SDK not loaded');
}

window.firebaseApp = firebase;

// ============================================================================
// FIRESTORE SHIM (Django Adapter)
// This intercepts all `firebase.firestore()` calls and routes them to Django.
// ============================================================================

class FirestoreShim {
  constructor() {
    this.FieldValue = {
      serverTimestamp: () => ({ _isFieldValue: true, _method: 'serverTimestamp' }),
      increment: (val) => ({ _isFieldValue: true, _method: 'increment', _value: val }),
      delete: () => ({ _isFieldValue: true, _method: 'delete' })
    };
  }

  async _fetch(endpoint, method = 'GET', data = null) {
    const user = firebase.auth().currentUser;
    const headers = { 'Content-Type': 'application/json' };
    
    if (user) {
      const token = await user.getIdToken();
      headers['Authorization'] = `Bearer ${token}`;
    }

    const options = { method, headers };
    if (data) options.body = JSON.stringify(data);

    const response = await fetch(`${API_BASE_URL}${endpoint}`, options);
    const result = await response.json();
    
    if (!response.ok) {
      throw new Error(result.error || `API Error: ${response.status}`);
    }
    return result;
  }

  collection(path) {
    return new CollectionRef(this, path);
  }
}

class CollectionRef {
  constructor(db, path) {
    this.db = db;
    this.path = path;
  }

  doc(id) {
    return new DocRef(this.db, `${this.path}/${id}`);
  }

  async add(data) {
    const parts = this.path.split('/');
    if (parts.length === 3 && parts[0] === 'users' && parts[2] === 'transactions') {
      // POST /api/users/{uid}/transactions/
      const uid = parts[1];
      return await this.db._fetch(`/users/${uid}/transactions/`, 'POST', data);
    } else if (parts[0] === 'withdrawals') {
      // POST /api/withdrawals/
      return await this.db._fetch(`/withdrawals/`, 'POST', data);
    }
    throw new Error(`Collection add not supported for path: ${this.path}`);
  }

  // Very basic query support just for referral code check during signup
  where(field, op, value) {
    return new Query(this.db, this.path, field, op, value);
  }
}

class Query {
  constructor(db, path, field, op, value) {
    this.db = db;
    this.path = path;
    this.field = field;
    this.op = op;
    this.value = value;
  }
  
  limit() { return this; }

  async get() {
    if (this.path === 'users' && this.field === 'referralCode') {
      try {
        const result = await this.db._fetch(`/users/by-referral/check/?code=${this.value}`);
        return {
          empty: false,
          docs: [{ id: result.uid, data: () => result }]
        };
      } catch (e) {
        return { empty: true, docs: [] };
      }
    }
    throw new Error(`Query not supported for: ${this.path}`);
  }
}

class DocRef {
  constructor(db, path) {
    this.db = db;
    this.path = path;
    const parts = path.split('/');
    this.uid = parts[0] === 'users' ? parts[1] : null;
  }

  collection(subPath) {
    return new CollectionRef(this.db, `${this.path}/${subPath}`);
  }

  async get() {
    const parts = this.path.split('/');
    
    // Admin check logic from signin.html
    if (parts[0] === 'admins') {
      try {
        // We use a special endpoint for admin verify
        const result = await this.db._fetch(`/admin/verify/`, 'POST');
        if (result.is_admin) {
          return { exists: true, data: () => result };
        }
        return { exists: false, data: () => null };
      } catch(e) {
        return { exists: false, data: () => null };
      }
    }

    if (this.uid) {
      try {
        const data = await this.db._fetch(`/users/${this.uid}/`);
        return { exists: true, data: () => data };
      } catch (e) {
        return { exists: false, data: () => null };
      }
    }
    throw new Error(`Get not supported for path: ${this.path}`);
  }

  async set(data) {
    if (this.uid) {
      return await this.db._fetch(`/users/${this.uid}/`, 'POST', data);
    }
    throw new Error(`Set not supported for path: ${this.path}`);
  }

  async update(data) {
    if (this.uid) {
      return await this.db._fetch(`/users/${this.uid}/`, 'PATCH', data);
    }
    // Update admin withdrawal status
    const parts = this.path.split('/');
    if (parts[0] === 'withdrawals') {
      const docId = parts[1];
      const action = data.status === 'completed' ? 'approve' : 'reject';
      return await this.db._fetch(`/admin/withdrawals/${docId}/`, 'PATCH', { action });
    }
    throw new Error(`Update not supported for path: ${this.path}`);
  }

  // Emulate real-time listener with polling
  onSnapshot(callback, errorCallback) {
    let isCancelled = false;
    
    const poll = async () => {
      if (isCancelled) return;
      try {
        const doc = await this.get();
        if (!isCancelled) callback(doc);
      } catch (e) {
        if (!isCancelled && errorCallback) errorCallback(e);
      }
      if (!isCancelled) {
        setTimeout(poll, 5000); // Poll every 5 seconds
      }
    };

    poll(); // Start polling
    
    // Return unsubscribe function
    return () => { isCancelled = true; };
  }
}

// Override firebase.firestore
const shim = new FirestoreShim();
firebase.firestore = () => shim;
firebase.firestore.FieldValue = shim.FieldValue;

// Create first admin account (run once in console)
async function createFirstAdmin(email, password) {
  try {
    const userCredential = await firebase.auth().createUserWithEmailAndPassword(email, password);
    const user = userCredential.user;
    console.log('Admin created in Auth:', user.uid);
    // Since firestore is shimmed, this will hit the normal endpoints, but we need
    // an admin endpoint to actually set the admin role. This is meant for console.
    console.warn("To make this user an admin, please use the Django admin panel or shell.");
    return user;
  } catch (error) {
    console.error('Error creating admin:', error);
  }
}
window.createFirstAdmin = createFirstAdmin;
