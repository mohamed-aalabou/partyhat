// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {FourFourToken} from "../contracts/FourFourToken.sol";

contract FourFourTokenTest is Test {
    FourFourToken internal token;

    address internal owner;
    address internal alice;
    address internal bob;

    uint256 internal constant FIXED_SUPPLY = 1_000_000_000e18;

    function setUp() public {
        owner = address(this);
        alice = makeAddr("alice");
        bob = makeAddr("bob");

        token = new FourFourToken("FourFour", "FOUR");
    }

    function test_constructor_initializesMetadataAndMintsFixedSupplyToDeployer() public {
        assertEq(token.name(), "FourFour");
        assertEq(token.symbol(), "FOUR");
        assertEq(token.decimals(), 18);

        assertEq(token.totalSupply(), FIXED_SUPPLY);
        assertEq(token.balanceOf(owner), FIXED_SUPPLY);

        assertEq(token.owner(), owner);
        assertFalse(token.paused());
    }

    function test_burn_decreasesTotalSupplyAndBalance() public {
        uint256 burnAmount = 123e18;

        uint256 supplyBefore = token.totalSupply();
        uint256 balBefore = token.balanceOf(owner);

        token.burn(burnAmount);

        assertEq(token.totalSupply(), supplyBefore - burnAmount);
        assertEq(token.balanceOf(owner), balBefore - burnAmount);
    }

    function test_burn_revertsIfAmountExceedsBalance() public {
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSignature(
                "ERC20InsufficientBalance(address,uint256,uint256)",
                alice,
                0,
                1
            )
        );
        token.burn(1);
    }

    function test_pause_onlyOwner() public {
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSignature("OwnableUnauthorizedAccount(address)", alice));
        token.pause();

        token.pause();
        assertTrue(token.paused());
    }

    function test_unpause_onlyOwner() public {
        token.pause();
        assertTrue(token.paused());

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSignature("OwnableUnauthorizedAccount(address)", alice));
        token.unpause();

        token.unpause();
        assertFalse(token.paused());
    }

    function test_pausedBlocksTransfer() public {
        token.transfer(alice, 10e18);

        token.pause();

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSignature("EnforcedPause()"));
        token.transfer(bob, 1e18);
    }

    function test_pausedBlocksTransferFrom() public {
        token.transfer(alice, 10e18);

        vm.prank(alice);
        token.approve(bob, 5e18);

        token.pause();

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSignature("EnforcedPause()"));
        token.transferFrom(alice, bob, 1e18);
    }

    function test_pausedBlocksBurn_dueToUpdateHook() public {
        token.transfer(alice, 10e18);
        token.pause();

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSignature("EnforcedPause()"));
        token.burn(1e18);
    }

    function test_unpauseRestoresTransferAndBurnFunctionality() public {
        token.transfer(alice, 10e18);

        token.pause();

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSignature("EnforcedPause()"));
        token.transfer(bob, 1e18);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSignature("EnforcedPause()"));
        token.burn(1e18);

        token.unpause();

        vm.prank(alice);
        token.transfer(bob, 1e18);
        assertEq(token.balanceOf(bob), 1e18);

        uint256 supplyBefore = token.totalSupply();
        vm.prank(alice);
        token.burn(2e18);
        assertEq(token.totalSupply(), supplyBefore - 2e18);
    }

    function test_allowanceAndTransferFrom_happyPathWhenUnpaused() public {
        token.transfer(alice, 100e18);

        vm.prank(alice);
        token.approve(bob, 40e18);

        assertEq(token.allowance(alice, bob), 40e18);

        vm.prank(bob);
        token.transferFrom(alice, bob, 15e18);

        assertEq(token.balanceOf(alice), 85e18);
        assertEq(token.balanceOf(bob), 15e18);
        assertEq(token.allowance(alice, bob), 25e18);
    }
}