// Js/firebase.js
// Earnova Firebase Configuration

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
  console.log('Firebase initialized successfully');
} else {
  console.warn('Firebase SDK not loaded');
}

// Export for use in other files
window.firebaseApp = firebase;

// Create first admin account (run once in console)
async function createFirstAdmin(email, password) {
  try {
    // Create user in Authentication
    const userCredential = await firebase.auth().createUserWithEmailAndPassword(email, password);
    const user = userCredential.user;
    
    // Store admin data in Firestore
    await firebase.firestore().collection('admins').doc(user.uid).set({
      email: email,
      role: 'admin',
      createdAt: firebase.firestore.FieldValue.serverTimestamp(),
      isActive: true
    });
    
    console.log('Admin created successfully:', user.uid);
    return user;
  } catch (error) {
    console.error('Error creating admin:', error);
  }
}

// Make it globally accessible for console use
window.createFirstAdmin = createFirstAdmin;
