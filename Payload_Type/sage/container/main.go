package main

import (
	// Standard
	"os"
	"path/filepath"

	// Mythic
	"github.com/MythicMeta/MythicContainer"
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"

	// Internal
	"github.com/MythicAgents/sage/Payload_Type/sage/container/commands"
	"github.com/MythicAgents/sage/Payload_Type/sage/container/payload/build"
)

func main() {
	logging.LogInfo("Starting Sage container")

	// Create a service for this Sage container
	payloadService := structs.AllPayloadData.Get("sage")

	// Build the Sage Payload container definition and add it
	// If running as standalone, locally, outside Mythic: export MYTHIC_SERVER_HOST=127.0.0.1
	payload, err := build.NewPayload()
	if err != nil {
		logging.LogError(err, "quitting")
		os.Exit(2)
	}
	payloadService.AddPayloadDefinition(payload)

	// Add the Sage payload build function definition
	payloadService.AddBuildFunction(build.Build)

	// Add the Sage agent commands
	for _, command := range commands.Commands() {
		payloadService.AddCommand(command)
	}

	// Get the Sage icon and add it
	payloadService.AddIcon(filepath.Join(".", "..", "sage.svg"))

	// Start the container
	MythicContainer.StartAndRunForever([]MythicContainer.MythicServices{MythicContainer.MythicServicePayload})
}
