// Traefik ships these extension types as a local module; the public Go proxy
// does not publish it. Keep the upstream declarations for the pinned tool.
package ext

// HTTP is a dynamic.HTTP extension.
type HTTP struct{}

// Router is a dynamic.Router extension.
type Router struct{}
