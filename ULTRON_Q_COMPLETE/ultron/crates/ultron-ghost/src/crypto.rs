//! Crypto primitives for the Ghost Network.
//!
//! Three concerns live here and nowhere else:
//!
//! 1. **Symmetric encryption** of inter-peer frames with AES-256-GCM.
//!    The on-wire layout is `nonce(12) || ciphertext || tag(16)`. The
//!    AEAD tag rides in the same buffer as the ciphertext per the
//!    RustCrypto API — we use `encrypt_in_place_append` style internally
//!    but expose a clean `encrypt(plaintext) -> Vec<u8>` surface.
//!
//! 2. **Key derivation** from the `ghost_secret` configured per LAN
//!    cluster. BLAKE3 in keyed-hash mode with a fixed context string
//!    derives a 32-byte AES key. Same `ghost_secret` on every peer
//!    produces the same key, so frames decrypt across nodes; different
//!    `ghost_secret` means different keys and invisibility.
//!
//! 3. **Sensitive-field scrubbing.** [`hash_sensitive`] is the canonical
//!    way to turn a privacy-sensitive string (window title, process
//!    name, file path) into a 16-byte BLAKE3 digest in lowercase hex.
//!    Receivers can match digests but never recover originals.
//!
//! ## Why the scrubber lives in this file
//!
//! Conceptually it's a privacy primitive, not a transport one — but it
//! shares the BLAKE3 dependency with the KDF, and keeping all hashing
//! in one place makes it trivial to audit "what algorithms does ULTRON
//! actually use" (`grep -r 'blake3' crates/ultron-ghost/src/`).

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use anyhow::{anyhow, Result};
use rand::RngCore;

/// On-wire nonce length. AES-GCM specifies 96 bits.
pub const NONCE_LEN: usize = 12;

/// AES-GCM authentication tag length. 128 bits.
pub const TAG_LEN: usize = 16;

/// Context string for the BLAKE3 KDF. Including the module name + a
/// version tag means a future scheme change can't be confused with the
/// current one — a future v2 would use a new context and produce
/// different keys from the same secret.
const KDF_CONTEXT: &str = "ULTRON.ghost.aes256gcm.v1";

/// Context string for stable per-machine IDs.
const SENDER_ID_CONTEXT: &str = "ULTRON.ghost.sender_id.v1";

/// Holds the derived AES key. Cheap to clone (32 bytes on the stack-ish
/// — `aes_gcm::Key` is a `GenericArray<u8, _>`).
#[derive(Clone)]
pub struct GhostCipher {
    cipher: Aes256Gcm,
}

impl GhostCipher {
    /// Derive a fresh cipher from the configured ghost secret. Cheap;
    /// safe to call at startup and stash for the rest of the process
    /// lifetime.
    pub fn from_secret(ghost_secret: &str) -> Result<Self> {
        let key_bytes = derive_key(ghost_secret);
        let key = Key::<Aes256Gcm>::from_slice(&key_bytes);
        Ok(Self {
            cipher: Aes256Gcm::new(key),
        })
    }

    /// Encrypt `plaintext` and return a wire-format buffer
    /// (`nonce || ciphertext || tag`).
    pub fn encrypt(&self, plaintext: &[u8]) -> Result<Vec<u8>> {
        // Fresh random nonce per frame. Reuse would be catastrophic for
        // GCM, so we use the OS CSPRNG; `OsRng` is non-blocking on all
        // supported targets.
        let mut nonce = [0u8; NONCE_LEN];
        rand::rngs::OsRng.fill_bytes(&mut nonce);
        let nonce_obj = Nonce::from_slice(&nonce);

        let ct_and_tag = self
            .cipher
            .encrypt(nonce_obj, plaintext)
            .map_err(|e| anyhow!("aes-gcm encrypt failed: {e:?}"))?;

        let mut out = Vec::with_capacity(NONCE_LEN + ct_and_tag.len());
        out.extend_from_slice(&nonce);
        out.extend_from_slice(&ct_and_tag);
        Ok(out)
    }

    /// Decrypt a wire-format buffer back to plaintext. Returns `Err` on
    /// any tampering or truncation — callers should `warn!` and drop
    /// the frame, never panic.
    pub fn decrypt(&self, wire: &[u8]) -> Result<Vec<u8>> {
        if wire.len() < NONCE_LEN + TAG_LEN {
            anyhow::bail!(
                "wire frame too short: {} < {}",
                wire.len(),
                NONCE_LEN + TAG_LEN
            );
        }
        let (nonce_bytes, ct_and_tag) = wire.split_at(NONCE_LEN);
        let nonce = Nonce::from_slice(nonce_bytes);
        let pt = self
            .cipher
            .decrypt(nonce, ct_and_tag)
            .map_err(|e| anyhow!("aes-gcm decrypt failed: {e:?}"))?;
        Ok(pt)
    }
}

/// Derive a 32-byte AES key from the configured ghost secret using
/// BLAKE3's keyed-context KDF. Exposed for testing; production code
/// goes through [`GhostCipher::from_secret`].
pub fn derive_key(ghost_secret: &str) -> [u8; 32] {
    blake3::derive_key(KDF_CONTEXT, ghost_secret.as_bytes())
}

/// Compute the stable sender ID for this machine. Deterministic given
/// `(hostname, ghost_secret)` — same machine always produces the same
/// ID across restarts.
///
/// Returns a 32-char lowercase hex string (BLAKE3 truncated to 16
/// bytes). Short enough to log without flooding lines; long enough that
/// collisions are practically impossible on a home LAN.
pub fn compute_sender_id(hostname: &str, ghost_secret: &str) -> String {
    let mut hasher = blake3::Hasher::new_derive_key(SENDER_ID_CONTEXT);
    hasher.update(hostname.as_bytes());
    hasher.update(b"\0"); // separator so "ab"+"cd" != "abc"+"d"
    hasher.update(ghost_secret.as_bytes());
    let mut out = [0u8; 16];
    let mut reader = hasher.finalize_xof();
    reader.fill(&mut out);
    hex::encode(out)
}

/// Hash a sensitive string for inclusion in a transmitted ghost frame.
///
/// Returns a 32-char lowercase hex digest. Receivers can compare two
/// digests for equality (= same string) but cannot recover the
/// original. We deliberately do NOT salt with the ghost_secret here so
/// the same window title produces the same digest across all peers — that's
/// the whole point: cross-peer correlation without leaking content.
///
/// Empty inputs return an empty string (not a hash of empty). This
/// preserves the "no data" signal — receivers can tell "the field
/// wasn't there" from "the field was there and had a value".
pub fn hash_sensitive(s: &str) -> String {
    if s.is_empty() {
        return String::new();
    }
    let digest = blake3::hash(s.as_bytes());
    let bytes = digest.as_bytes();
    hex::encode(&bytes[..16])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encrypt_decrypt_roundtrip() {
        let cipher = GhostCipher::from_secret("hunter2-ultra-secret").unwrap();
        let plaintext = b"a quick brown fox jumps over the lazy dog";
        let wire = cipher.encrypt(plaintext).unwrap();
        assert!(wire.len() > plaintext.len(), "ciphertext should be longer than plaintext");
        let decoded = cipher.decrypt(&wire).unwrap();
        assert_eq!(decoded, plaintext);
    }

    #[test]
    fn different_secrets_cannot_decrypt_each_other() {
        let a = GhostCipher::from_secret("secret-A").unwrap();
        let b = GhostCipher::from_secret("secret-B").unwrap();
        let wire = a.encrypt(b"hello").unwrap();
        let r = b.decrypt(&wire);
        assert!(r.is_err(), "frame from peer A must NOT decrypt with peer B's key");
    }

    #[test]
    fn tampered_ciphertext_is_rejected() {
        let cipher = GhostCipher::from_secret("hunter2").unwrap();
        let mut wire = cipher.encrypt(b"payload").unwrap();
        // Flip a bit somewhere in the middle of the ciphertext (after
        // the 12-byte nonce, before the tag).
        let idx = NONCE_LEN + 2;
        wire[idx] ^= 0x01;
        let r = cipher.decrypt(&wire);
        assert!(r.is_err(), "tampered frame must be rejected");
    }

    #[test]
    fn truncated_wire_is_rejected() {
        let cipher = GhostCipher::from_secret("hunter2").unwrap();
        let wire = cipher.encrypt(b"payload").unwrap();
        // Truncate before nonce ends.
        let short = &wire[..5];
        assert!(cipher.decrypt(short).is_err());
        // Truncate inside tag.
        let mid = &wire[..wire.len() - 4];
        assert!(cipher.decrypt(mid).is_err());
    }

    #[test]
    fn each_encrypt_uses_fresh_nonce() {
        let cipher = GhostCipher::from_secret("hunter2").unwrap();
        let a = cipher.encrypt(b"payload").unwrap();
        let b = cipher.encrypt(b"payload").unwrap();
        // Same plaintext, same key — different ciphertext because the
        // nonce is fresh each time. (If this ever fails we have a
        // catastrophic key-stream reuse bug.)
        assert_ne!(a, b);
        // And nonces themselves should differ.
        assert_ne!(&a[..NONCE_LEN], &b[..NONCE_LEN]);
    }

    #[test]
    fn derive_key_is_deterministic() {
        let k1 = derive_key("the-secret");
        let k2 = derive_key("the-secret");
        assert_eq!(k1, k2);
        let k3 = derive_key("the-secret-2");
        assert_ne!(k1, k3);
    }

    #[test]
    fn compute_sender_id_is_stable() {
        let a = compute_sender_id("LAPTOP-HM36HMQC", "secret");
        let b = compute_sender_id("LAPTOP-HM36HMQC", "secret");
        assert_eq!(a, b);
        assert_eq!(a.len(), 32, "16 bytes hex-encoded = 32 chars");
    }

    #[test]
    fn compute_sender_id_differs_per_host_and_secret() {
        let a = compute_sender_id("hostA", "secret");
        let b = compute_sender_id("hostB", "secret");
        let c = compute_sender_id("hostA", "different-secret");
        assert_ne!(a, b);
        assert_ne!(a, c);
        assert_ne!(b, c);
    }

    #[test]
    fn compute_sender_id_resists_concat_ambiguity() {
        // Without the null separator these would collide:
        //   hostname="ab", secret="cd"   vs.  hostname="abc", secret="d"
        let a = compute_sender_id("ab", "cd");
        let b = compute_sender_id("abc", "d");
        assert_ne!(a, b, "null separator must prevent boundary ambiguity");
    }

    #[test]
    fn hash_sensitive_is_deterministic_and_short() {
        let a = hash_sensitive("Visual Studio Code - main.rs");
        let b = hash_sensitive("Visual Studio Code - main.rs");
        assert_eq!(a, b);
        assert_eq!(a.len(), 32);
        // And the digest must NOT contain the original string.
        assert!(!a.contains("Visual"));
        assert!(!a.contains("main"));
    }

    #[test]
    fn hash_sensitive_empty_in_empty_out() {
        assert_eq!(hash_sensitive(""), "");
    }

    #[test]
    fn hash_sensitive_distinguishes_strings() {
        let a = hash_sensitive("Code.exe");
        let b = hash_sensitive("chrome.exe");
        assert_ne!(a, b);
    }
}
