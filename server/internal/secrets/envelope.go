// Package secrets provides instance-key envelopes with a distinct context for each credential.
package secrets

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"errors"

	"animemo.local/server/internal/fault"
)

func Cipher(key []byte) (cipher.AEAD, error) {
	if len(key) == 0 {
		return nil, nil
	}
	if len(key) != 32 {
		return nil, errors.New("ANIMEMO_SECRET_KEY must contain 32 bytes")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}
func Seal(box cipher.AEAD, context string, plain []byte) []byte {
	nonce := make([]byte, box.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		panic(err)
	}
	return box.Seal(nonce, nonce, plain, []byte(context))
}
func Open(box cipher.AEAD, context string, value []byte) ([]byte, error) {
	if box == nil || len(value) < box.NonceSize() {
		return nil, fault.New("service_unavailable", "实例加密密钥不可用，请联系部署者恢复原密钥。")
	}
	n := box.NonceSize()
	plain, err := box.Open(nil, value[:n], value[n:], []byte(context))
	if err != nil {
		return nil, fault.New("service_unavailable", "实例加密密钥不匹配，请联系部署者恢复原密钥。")
	}
	return plain, nil
}
