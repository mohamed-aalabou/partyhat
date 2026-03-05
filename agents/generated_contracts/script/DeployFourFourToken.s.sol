// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {FourFourToken} from "../contracts/FourFourToken.sol";

contract DeployFourFourToken is Script {
    function run() external returns (FourFourToken token) {
        require(block.chainid == 43113, "DeployFourFourToken: not Avalanche Fuji (43113)");

        vm.startBroadcast(); // Uses --private-key or default sender configured by Foundry
        token = new FourFourToken("four-four", "FOUR"); // Mints fixed supply to msg.sender (deployer)
        vm.stopBroadcast();
    }
}
